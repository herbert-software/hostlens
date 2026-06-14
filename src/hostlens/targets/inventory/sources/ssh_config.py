"""``ssh_config`` inventory source — hand-written OpenSSH config line parser.

Spec: ``inventory-source/spec.md`` §需求:`ssh_config` source.

This is a **hand-written line parser**, deliberately NOT delegated to
``asyncssh.config.SSHClientConfig``: the SDK parser auto-resolves
``Include`` / ``Match`` / wildcards for connection time and offers no hook
to impose the ``~/.ssh/`` boundary + realpath check, so it would silently
bypass the ``include_path_escape`` security gate.

Key invariants:

- Connection address comes from the ``HostName`` literal — never DNS
  resolution of the ``Host`` alias (defeats FakeDNS / split-horizon). When
  ``HostName`` is absent the canonical ``Host`` token is used verbatim
  (OpenSSH fallback; asyncssh resolves it at connect time).
- ``Include`` is bounded to the ``~/.ssh/`` tree by a two-gate check
  (realpath pre-screen via ``commonpath`` + ``O_NOFOLLOW`` final read to
  close the TOCTOU window). One level of ``Include`` only.
- ``IdentityFile`` becomes a ``key_path`` reference: ``~`` is expanded but
  any ``${VAR}`` fails closed (never ``expandvars``); the file is never
  opened / stat-ed at parse time.
- ``Match`` blocks and wildcard ``Host`` patterns are skipped + logged.
"""

from __future__ import annotations

import os

import structlog

from hostlens.core.exceptions import ConfigError
from hostlens.targets.inventory.models import CandidateTarget, normalize_target_name

__all__ = ["SshConfigSource"]

_logger = structlog.get_logger(__name__)

# How many leading non-empty, non-comment lines we scan for OpenSSH
# directives during ``can_handle`` content sniffing. Bounded so sniffing a
# large unrelated file stays cheap.
_SNIFF_MAX_LINES: int = 40

# OpenSSH directive prefixes that flag a file as ssh_config during content
# sniffing (case-insensitive, matched on the first token of a line).
_SNIFF_KEYWORDS: frozenset[str] = frozenset({"host", "hostname", "match"})


def _is_wildcard_host(token: str) -> bool:
    """``Host`` pattern with a glob (``*`` / ``?``) — skipped (not explicit)."""

    return "*" in token or "?" in token


def _split_directive(line: str) -> tuple[str, str] | None:
    """Split a config line into ``(keyword_lower, rest)`` or ``None``.

    Returns ``None`` for blank / comment lines. OpenSSH accepts ``Key
    Value`` and ``Key=Value``; the keyword is matched case-insensitively
    (OpenSSH keywords are case-insensitive, values are not).
    """

    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    # ``Key=Value`` form: split on the first ``=`` only when it precedes any
    # whitespace, otherwise fall through to whitespace splitting.
    if "=" in stripped and (" " not in stripped or stripped.index("=") < stripped.index(" ")):
        keyword, _, rest = stripped.partition("=")
        return keyword.strip().lower(), rest.strip()
    keyword, _, rest = stripped.partition(" ")
    return keyword.lower(), rest.strip()


def _first_token(value: str) -> str:
    """Return the first whitespace-separated token of a directive value."""

    return value.split()[0] if value.split() else ""


class SshConfigSource:
    """Parses OpenSSH client config into ``CandidateTarget`` list."""

    name = "ssh_config"

    def can_handle(self, ref: str) -> bool:
        """Sniff ``ref`` as an ssh_config file.

        Match when the basename matches ``*config`` or equals ``hosts``
        (covers ``~/.ssh/config`` and ``~/tizi/hosts``), OR the first few
        non-empty / non-comment lines contain an OpenSSH directive
        (``Host`` / ``HostName`` / ``Match``).
        """

        basename = os.path.basename(ref)
        if basename.endswith("config") or basename == "hosts":
            return True
        try:
            text = self._read_ref(ref)
        except OSError:
            return False
        scanned = 0
        for line in text.splitlines():
            directive = _split_directive(line)
            if directive is None:
                continue
            scanned += 1
            if directive[0] in _SNIFF_KEYWORDS:
                return True
            if scanned >= _SNIFF_MAX_LINES:
                break
        return False

    def parse(self, ref: str) -> list[CandidateTarget]:
        """Parse ``ref`` (and one level of in-tree ``Include``) into candidates."""

        text = self._read_ref(ref)
        return self._parse_text(text, allow_include=True)

    # -- internal -----------------------------------------------------------

    @staticmethod
    def _read_ref(ref: str) -> str:
        path = os.path.expanduser(ref)
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            raise ConfigError(
                "failed to read ssh_config source",
                kind="ssh_config_read_error",
                path=path,
                original=exc,
            ) from exc

    def _parse_text(self, text: str, *, allow_include: bool) -> list[CandidateTarget]:
        candidates: list[CandidateTarget] = []
        block: dict[str, str] | None = None
        aliases: list[str] = []
        skip_block = False

        def flush() -> None:
            nonlocal block, aliases, skip_block
            if block is not None and not skip_block:
                candidates.append(self._build_candidate(aliases, block))
            block = None
            aliases = []
            skip_block = False

        for raw_line in text.splitlines():
            directive = _split_directive(raw_line)
            if directive is None:
                continue
            keyword, value = directive

            if keyword == "host":
                flush()
                tokens = value.split()
                if any(_is_wildcard_host(token) for token in tokens):
                    _logger.debug("skipping wildcard Host pattern", host=value)
                    skip_block = True
                    block = {}
                    continue
                aliases = tokens
                block = {}
                continue

            if keyword == "match":
                flush()
                _logger.debug("skipping Match block")
                skip_block = True
                block = {}
                continue

            if keyword == "include":
                if not allow_include:
                    _logger.debug("skipping nested Include (one level only)")
                    continue
                included = self._read_include(value)
                candidates.extend(self._parse_text(included, allow_include=False))
                continue

            if block is not None and not skip_block:
                block[keyword] = value

        flush()
        return candidates

    @staticmethod
    def _build_candidate(aliases: list[str], block: dict[str, str]) -> CandidateTarget:
        # Canonical alias = the last token of the ``Host`` line (OpenSSH
        # treats it as the canonical name; tizi ``Host bwg bandwagon`` →
        # ``bandwagon``).
        canonical_raw = aliases[-1]
        name = normalize_target_name(canonical_raw)

        host_name = block.get("hostname")
        host = _first_token(host_name) if host_name else canonical_raw

        user_value = block.get("user")
        user = _first_token(user_value) if user_value else None

        port_value = block.get("port")
        port = int(_first_token(port_value)) if port_value else None

        identity = block.get("identityfile")
        key_path = _resolve_identity_file(_first_token(identity)) if identity else None

        return CandidateTarget(
            name=name,
            type="ssh",
            host=host,
            user=user,
            port=port,
            key_path=key_path,
            source_metadata={"source": "ssh_config", "raw_identifier": canonical_raw},
        )

    @staticmethod
    def _read_include(value: str) -> str:
        """Read an ``Include`` target, fail-closed outside the ``~/.ssh/`` tree.

        Two gates (spec §需求:`Include` 路径边界):

        1. realpath pre-screen — ``base = realpath(~/.ssh)`` (left operand
           also realpath'd so a ``~/.ssh`` that is itself a symlink is not
           mis-rejected); ``commonpath([base, tgt]) != base`` rejects.
        2. ``O_NOFOLLOW`` final read — the last hop must not be a symlink,
           closing the TOCTOU window where realpath validates then the
           attacker re-points the symlink.

        ``commonpath`` raising ``ValueError`` (mixed abs/rel; unreachable
        after realpath but a fail-closed bound) maps to
        ``include_path_escape`` too. The exception text NEVER echoes file
        content — only the path ``kind``.
        """

        include_token = _first_token(value)
        expanded = os.path.expanduser(include_token)

        base = os.path.realpath(os.path.expanduser("~/.ssh"))
        target = os.path.realpath(expanded)
        try:
            common = os.path.commonpath([base, target])
        except ValueError as exc:
            raise ConfigError(
                "Include path escapes the ~/.ssh/ tree",
                kind="include_path_escape",
            ) from exc
        if common != base:
            raise ConfigError(
                "Include path escapes the ~/.ssh/ tree",
                kind="include_path_escape",
            )

        try:
            fd = os.open(expanded, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ConfigError(
                "Include path is a symlink or unreadable",
                kind="include_path_escape",
            ) from exc
        try:
            with os.fdopen(fd, encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            raise ConfigError(
                "failed to read Include target",
                kind="include_path_escape",
            ) from exc


def _resolve_identity_file(value: str) -> str:
    """Expand ``~`` in an ``IdentityFile`` reference; fail closed on ``${VAR}``.

    ``key_path`` is a non-secret field that lands on disk as a literal
    value (it does NOT enjoy the loader's ``${VAR}`` placeholder
    preservation). Letting the source ``expandvars`` here would smuggle an
    entire env value (possibly a sensitive ``/run/.../secrets/...`` path)
    into a plaintext-persisted ``key_path`` — fail-closed rejection is
    safer than expansion. The path is never opened / stat-ed.
    """

    if "${" in value:
        raise ConfigError(
            "IdentityFile contains a ${VAR} placeholder; key_path must be a literal path",
            kind="key_path_placeholder_forbidden",
        )
    return os.path.expanduser(value)
