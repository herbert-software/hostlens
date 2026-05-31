#!/usr/bin/env python3
"""Lint Hostlens cassette files for secrets and schema drift.

Two modes, mutually exclusive:

1. Scan (default): walk every committed cassette — the flat
   ``tests/fixtures/cassettes/*.jsonl`` files AND the migrated incident
   cassettes under ``src/hostlens/demo/scenarios/**/cassette.jsonl`` (now public
   wheel content, so the secret-scan matters more, not less). Each record's
   ``response`` field is validated against ``MessageResponse``,
   and reject any line whose raw string matches an extended set of
   sensitive-data patterns (Anthropic / generic ``sk-`` keys, Bearer
   tokens, JWTs, ``password=`` / ``api_key=`` assignments, absolute home
   paths, ``.ssh`` paths, IPv4 addresses, hostname-like FQDNs, email
   addresses). Sensitive-substring hits → ``exit 1`` with a ``stderr``
   message naming the matched pattern. Schema validation failure →
   ``exit 1``. Clean → ``exit 0``.

2. ``--check-schema-drift --current-tools-hash <hex>``: cross-check the
   optional ``tools_schema_hash`` field on each cassette record against
   the supplied current hash. Drift produces a ``stdout`` warning but
   never sets a non-zero exit. The flag REQUIRES
   ``--current-tools-hash``; omitting it → ``exit 2`` with a stderr
   error (the spec rejects silent skips).

The lint is intentionally a standalone script: it imports only the
narrow ``hostlens.core.redact`` (shared sensitive-pattern set),
``hostlens.agent.backend`` (``MessageResponse`` schema), and
``hostlens.agent.cassette_key`` (single-source request-key helper, used
to flag duplicate request-keys within a file) symbols needed to validate
cassettes, and refuses to import the wider ``hostlens.tools`` /
``hostlens.agent.backends`` packages so a CI job can run it without
provisioning the full Agent runtime. The tools-schema hash for
``--check-schema-drift`` is computed externally and injected via
``--current-tools-hash``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

from hostlens.agent.backend import MessageResponse
from hostlens.agent.cassette_key import request_key_for_payload
from hostlens.core.redact import detect_sensitive_text, redact_text

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASSETTE_DIR = REPO_ROOT / "tests" / "fixtures" / "cassettes"
# The migrated incident cassettes live under the demo package as
# ``<key>/cassette.jsonl`` (design D2). Default scan covers this scoped subtree
# in addition to the flat fixtures dir — NOT a blind repo-root walk.
DEFAULT_DEMO_SCENARIOS_DIR = REPO_ROOT / "src" / "hostlens" / "demo" / "scenarios"


class LintError(Exception):
    """A cassette failed scan-mode validation.

    Carries enough context (file, line, reason) for the script's top-level
    handler to render a single human-readable message on stderr.
    """

    def __init__(self, *, path: Path, line_no: int, reason: str) -> None:
        super().__init__(f"{path}:{line_no}: {reason}")
        self.path = path
        self.line_no = line_no
        self.reason = reason


def iter_cassette_files(directory: Path) -> Iterator[Path]:
    """Yield every ``*.jsonl`` file directly under ``directory`` in sorted order."""

    if not directory.is_dir():
        return
    yield from sorted(directory.glob("*.jsonl"))


def iter_default_cassette_files() -> Iterator[Path]:
    """Yield every committed cassette the default (no-arg) scan must cover.

    Two scoped roots, deduplicated and globally sorted: the flat
    ``tests/fixtures/cassettes/*.jsonl`` and the migrated incident cassettes at
    ``src/hostlens/demo/scenarios/**/cassette.jsonl``. This is the set CI runs
    with no args — keeping the migrated incident cassettes inside the secret
    gate. It is NOT a blind repo-root walk.
    """

    seen: set[Path] = set()
    for path in iter_cassette_files(DEFAULT_CASSETTE_DIR):
        seen.add(path)
    if DEFAULT_DEMO_SCENARIOS_DIR.is_dir():
        for path in DEFAULT_DEMO_SCENARIOS_DIR.glob("*/cassette.jsonl"):
            seen.add(path)
    yield from sorted(seen)


def scan_line_for_sensitive_substrings(line: str) -> str | None:
    """Return the name of the first sensitive pattern matched in ``line``.

    Uses both the shared ``hostlens.core.redact.detect_sensitive_text``
    (which walks ``CASSETTE_SENSITIVE_PATTERNS`` — the same rule set
    ``RecordingBackend`` uses, so "recorded then linted" stays consistent)
    and the ``hostlens.core.redact.redact_text`` baseline: if
    ``detect_sensitive_text`` misses but ``redact_text`` still rewrites the
    line, the line contained a secret per the runtime redaction rules.
    """

    hit = detect_sensitive_text(line)
    if hit is not None:
        return hit
    if redact_text(line) != line:
        return "redact_text_baseline"
    return None


def validate_record_schema(record: dict[str, object], *, path: Path, line_no: int) -> None:
    """Raise ``LintError`` if ``record["response"]`` is not a valid
    ``MessageResponse`` shape.

    The validator deliberately accepts records missing ``request`` /
    ``response`` keys and reports them with named reasons so a malformed
    cassette never silently passes lint.
    """

    if "response" not in record:
        raise LintError(path=path, line_no=line_no, reason="record missing 'response' key")
    try:
        MessageResponse.model_validate(record["response"])
    except Exception as exc:
        raise LintError(
            path=path,
            line_no=line_no,
            reason=f"response failed MessageResponse validation: {type(exc).__name__}",
        ) from exc


def request_key_for_record(record: dict[str, object]) -> str | None:
    """Compute the request-key for a cassette ``record`` via the shared helper.

    Reads the canonical ``request`` subset (``model`` / ``messages`` /
    ``tools_count``) and delegates to ``cassette_key.request_key_for_payload``
    — the same single-source keying used by ``PlaybackBackend`` (lookup) and
    ``RecordingBackend`` (write), so the duplicate-key check cannot drift from
    what playback actually collides on. Returns ``None`` when ``request`` is
    absent or not shaped as the canonical subset (such records are out of
    scope for the duplicate check; schema validation guards ``response``).
    """

    request = record.get("request")
    if not isinstance(request, dict):
        return None
    model = request.get("model")
    messages = request.get("messages")
    tools_count = request.get("tools_count")
    if not isinstance(model, str) or not isinstance(messages, list):
        return None
    if not isinstance(tools_count, int) or isinstance(tools_count, bool):
        return None
    return request_key_for_payload(model, messages, tools_count)


def scan_cassette_file(path: Path) -> None:
    """Run scan-mode checks on a single cassette file.

    Raises ``LintError`` on the first failing line so the script aborts
    early — running through all lines after a failure would just add
    noise.

    Besides per-line secret / schema checks, accumulates each record's
    request-key within the file: a repeated key means ``PlaybackBackend``
    would silently serve the first matching record and swallow the rest, so
    a duplicate within one cassette is a hard ``LintError``.
    """

    seen_keys: dict[str, int] = {}
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            hit = scan_line_for_sensitive_substrings(line)
            if hit is not None:
                raise LintError(
                    path=path,
                    line_no=line_no,
                    reason=f"sensitive substring detected: {hit}",
                )
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LintError(
                    path=path,
                    line_no=line_no,
                    reason=f"invalid JSON: {exc.msg}",
                ) from exc
            if not isinstance(record, dict):
                raise LintError(
                    path=path,
                    line_no=line_no,
                    reason=f"record not a JSON object (got {type(record).__name__})",
                )
            validate_record_schema(record, path=path, line_no=line_no)
            key = request_key_for_record(record)
            if key is not None:
                first_line = seen_keys.get(key)
                if first_line is not None:
                    raise LintError(
                        path=path,
                        line_no=line_no,
                        reason=(
                            f"duplicate request-key {key[:12]}... "
                            f"(first seen at line {first_line}); "
                            "PlaybackBackend would silently serve only the first record"
                        ),
                    )
                seen_keys[key] = line_no


def check_schema_drift(paths: Iterable[Path], *, current_hash: str) -> None:
    """Compare every cassette's ``tools_schema_hash`` against ``current_hash``.

    Drift produces a stdout warning naming the cassette, the stored hash,
    and the current hash. The function returns without raising on drift —
    schema drift is a soft signal for a reviewer to consider re-recording
    a cassette, not a CI-blocking failure.
    """

    for path in paths:
        with path.open("r", encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, start=1):
                line = raw.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # Skip — scan mode reports this as a hard error
                    # separately; here we only care about drift.
                    continue
                if not isinstance(record, dict):
                    continue
                cassette_hash = record.get("tools_schema_hash")
                if cassette_hash is None:
                    continue
                if cassette_hash != current_hash:
                    print(
                        f"WARNING: tools_schema_hash drift in cassette {path}:{line_no}: "
                        f"cassette={cassette_hash} current={current_hash}"
                    )


def build_argument_parser() -> argparse.ArgumentParser:
    """Return the CLI argument parser.

    Defined as a function so the unit tests can exercise parsing logic
    without invoking ``main`` itself (which has side effects on argv /
    exit codes).
    """

    parser = argparse.ArgumentParser(
        description="Lint Hostlens cassettes for secrets and tools schema drift.",
    )
    parser.add_argument(
        "--cassette-dir",
        type=Path,
        default=None,
        help=(
            "Scan only this directory's flat *.jsonl files. Omit to scan the "
            "default committed set (tests/fixtures/cassettes + "
            "src/hostlens/demo/scenarios/**/cassette.jsonl)."
        ),
    )
    parser.add_argument(
        "--check-schema-drift",
        action="store_true",
        help="Switch to drift-check mode; requires --current-tools-hash.",
    )
    parser.add_argument(
        "--current-tools-hash",
        type=str,
        default=None,
        help="SHA-256 hex of the current registered tools schema (drift mode only).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code (0 / 1 / 2)."""

    parser = build_argument_parser()
    args = parser.parse_args(argv)

    cassette_dir: Path | None = args.cassette_dir
    if cassette_dir is None:
        files = list(iter_default_cassette_files())
    else:
        files = list(iter_cassette_files(cassette_dir))

    if args.check_schema_drift:
        if args.current_tools_hash is None:
            print(
                "error: --current-tools-hash required when using --check-schema-drift",
                file=sys.stderr,
            )
            return 2
        check_schema_drift(files, current_hash=args.current_tools_hash)
        return 0

    # Scan mode.
    try:
        for path in files:
            scan_cassette_file(path)
    except LintError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
