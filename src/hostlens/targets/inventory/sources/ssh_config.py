"""``ssh_config`` inventory source — hand-written OpenSSH config line parser.

Spec: ``inventory-source/spec.md`` §需求:`ssh_config` source.

This is a **hand-written line parser**, deliberately NOT delegated to
``asyncssh.config.SSHClientConfig``: the SDK parser auto-resolves
``Include`` / ``Match`` / wildcards for connection time and offers no hook
to impose the path boundary + realpath check, so it would silently bypass
the ``include_path_escape`` security gate.

Key invariants:

- Connection address comes from the ``HostName`` literal — never DNS
  resolution of the ``Host`` alias (defeats FakeDNS / split-horizon). When
  ``HostName`` is absent the canonical ``Host`` token is used verbatim
  (OpenSSH fallback; asyncssh resolves it at connect time).
- ``Include`` resolves OpenSSH-style (``~`` expanded, relative anchored to
  ``~/.ssh``, globs expanded) and is bounded to the home tree or the
  resolved ``~/.ssh`` tree by a realpath ``commonpath`` check + ``O_NOFOLLOW``
  final read (closes the TOCTOU window). One level of ``Include`` only.
- ``IdentityFile`` becomes a ``key_path`` reference: ``~`` is expanded but
  any ``${VAR}`` fails closed (never ``expandvars``); the file is never
  opened / stat-ed at parse time.
- ``Match`` blocks and wildcard ``Host`` patterns are skipped + logged.
- Directives before the first ``Host`` / ``Match`` are OpenSSH globals — applied
  as defaults to every host (an explicit host-specific directive still wins).
"""

from __future__ import annotations

import glob
import os

import structlog

from hostlens.core.exceptions import ConfigError
from hostlens.targets.inventory.models import (
    CandidateTarget,
    normalize_target_name,
    reject_normalized_name_collisions,
    resolve_key_path,
)

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
        pending, defaults = self._parse_text(text, allow_include=True)
        # Build deferred to here so ``Host *`` defaults apply to every host
        # uniformly — including Include'd hosts (merged into ``pending``) and a
        # ``Host *`` block that appears after the hosts or inside an Include.
        # The explicit host-specific directive wins over a default.
        candidates = [self._build_candidate(a, {**defaults, **b}) for a, b in pending]
        reject_normalized_name_collisions(candidates)
        return candidates

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

    def _parse_text(
        self, text: str, *, allow_include: bool
    ) -> tuple[list[tuple[list[str], dict[str, str]]], dict[str, str]]:
        """Return ``(explicit-host blocks, Host-* defaults)`` — building deferred.

        Construction is deferred to ``parse`` so ``Host *`` defaults apply
        uniformly. An ``Include``'s blocks + defaults are merged into this
        file's, so the parent's ``Host *`` reaches Include'd hosts too.
        Directives appearing before the first ``Host`` / ``Match`` are OpenSSH
        globals (an implicit ``Host *``) — seeded into ``defaults`` here so they
        are not silently dropped.
        """

        pending: list[tuple[list[str], dict[str, str]]] = []
        defaults: dict[str, str] = {}  # directives under ``Host *`` / pre-Host globals
        # Seed an open default block so directives before the first Host / Match
        # accumulate as OpenSSH globals instead of being discarded.
        block: dict[str, str] | None = {}
        aliases: list[str] = []
        mode = "default"  # "host" | "default" | "skip"

        def flush() -> None:
            nonlocal block, aliases, mode
            if block is not None:
                if mode == "host":
                    pending.append((aliases, block))
                elif mode == "default":
                    defaults.update(block)
            block = None
            aliases = []
            mode = ""

        for raw_line in text.splitlines():
            directive = _split_directive(raw_line)
            if directive is None:
                continue
            keyword, value = directive

            if keyword == "host":
                flush()
                tokens = value.split()
                if not tokens:
                    raise ConfigError(
                        "ssh_config 'Host' line has no pattern",
                        kind="invalid_ssh_config",
                    )
                block = {}
                if tokens == ["*"]:
                    # ``Host *`` matches every host — its directives are global
                    # defaults applied to each host (an explicit host-specific
                    # directive wins; full first-match ordering is not modelled).
                    mode = "default"
                elif any(_is_wildcard_host(token) for token in tokens):
                    # Other wildcard patterns (``*.x`` / ``?``) need pattern
                    # matching we do not implement — skip + log.
                    _logger.debug("skipping wildcard Host pattern", host=value)
                    mode = "skip"
                else:
                    aliases = tokens
                    mode = "host"
                continue

            if keyword == "match":
                flush()
                _logger.debug("skipping Match block")
                block = {}
                mode = "skip"
                continue

            if keyword == "include":
                if not allow_include:
                    _logger.debug("skipping nested Include (one level only)")
                    continue
                included = self._read_include(value)
                inc_pending, inc_defaults = self._parse_text(included, allow_include=False)
                pending.extend(inc_pending)
                defaults.update(inc_defaults)
                continue

            if block is not None and mode in ("host", "default"):
                block[keyword] = value

        flush()
        return pending, defaults

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
        port: int | None
        if port_value:
            port_token = _first_token(port_value)
            try:
                port = int(port_token)
            except ValueError as exc:
                raise ConfigError(
                    f"invalid ssh_config Port: {port_token!r}",
                    kind="invalid_ssh_config",
                ) from exc
        else:
            port = None

        identity = block.get("identityfile")
        key_path = resolve_key_path(_first_token(identity)) if identity else None

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
        """Read ``Include`` target(s) the way OpenSSH resolves them, fail-closed.

        Resolution mirrors OpenSSH user-config semantics:

        - ``~`` is expanded; a **relative** path is anchored to ``~/.ssh/``
          (OpenSSH's rule), never the process CWD.
        - Shell **globs** (``Include ~/.ssh/config.d/*``) are expanded; a glob
          matching nothing is treated as empty (OpenSSH ignores it).

        Safety (the ssh_config is operator-trusted, same as ``target add``):
        each resolved path must stay within the user's **home tree** or the
        symlink-resolved ``~/.ssh`` tree — this allows the documented
        ``Include ~/tizi/hosts`` pattern and ``~/.ssh`` dotfiles symlinks while
        blocking ``Include /etc/shadow``. The boundary is checked on the
        pattern itself (rejects an escaping path regardless of existence) and
        on each glob match. The final read uses ``O_NOFOLLOW`` (closes the
        TOCTOU / last-hop-symlink window in a shared config dir). The exception
        text NEVER echoes file content — only the path ``kind``.
        """

        expanded = os.path.expanduser(_first_token(value))
        if not os.path.isabs(expanded):
            # OpenSSH anchors relative Include paths to ~/.ssh, not the CWD.
            expanded = os.path.join(os.path.expanduser("~/.ssh"), expanded)

        roots = [
            os.path.realpath(os.path.expanduser("~")),
            os.path.realpath(os.path.expanduser("~/.ssh")),
        ]
        if not _within_roots(os.path.realpath(expanded), roots):
            raise ConfigError("Include path escapes the allowed tree", kind="include_path_escape")

        contents: list[str] = []
        for path in sorted(glob.glob(expanded)):
            if not _within_roots(os.path.realpath(path), roots):
                raise ConfigError(
                    "Include path escapes the allowed tree", kind="include_path_escape"
                )
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError as exc:
                raise ConfigError(
                    "Include path is a symlink or unreadable",
                    kind="include_path_escape",
                ) from exc
            try:
                with os.fdopen(fd, encoding="utf-8") as handle:
                    contents.append(handle.read())
            except OSError as exc:
                raise ConfigError(
                    "failed to read Include target",
                    kind="include_path_escape",
                ) from exc
        return "\n".join(contents)


def _within_roots(real_path: str, roots: list[str]) -> bool:
    """True iff ``real_path`` is inside any of ``roots`` (component-level)."""

    for root in roots:
        try:
            if os.path.commonpath([root, real_path]) == root:
                return True
        except ValueError:
            continue
    return False
