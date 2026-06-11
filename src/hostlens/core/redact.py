"""Text-level secret redaction utility.

`redact_text(s)` applies the default regex rules from
`docs/OPERABILITY.md` §7.2 to a single string and returns a redacted copy.
Each matched secret is replaced with its first 4 and last 4 characters
joined by `...` (`sk-abcd...7890`); strings shorter than 9 characters are
fully masked as `****`.

The function is purely functional and stateless. It is invoked at any
rendering boundary that writes user-visible output (markdown / json
report, log lines, notifier payloads).
"""

from __future__ import annotations

import re

__all__ = ["CASSETTE_SENSITIVE_PATTERNS", "detect_sensitive_text", "redact_text"]


# Compiled once at import time; OPERABILITY.md §7.2 default rule set.
_KEYWORD_ASSIGN = re.compile(
    r"(?i)\b(password|secret|token|api[_-]?key|bearer)\s*[:=]\s*"
    r"((?:[^\s\"']+|\"(?:\\.|[^\"\\])*\"?|'[^']*'?)+)"
)
"""Matches `key:value` / `key=value` form, e.g. `password=<the-value>`
or `api_key: sk-<the-value>`. The regex requires a `:` or `=` separator
between keyword and value; the bare HTTP-header form `Bearer <token>`
(space-separated) is handled by `_BEARER_HEADER` below.

Group 2 is the same quote-aware shell-word fragment used for flag-form
values (`_SHELL_WORD`): a bare `(\\S+)` truncates a quoted value at the
in-quote space (`password="a b"` -> masks only `"a`, leaks `b"`), so the
value run absorbs whole quoted spans. Masking goes through
`_mask_glued_value` (quote-stripped, re-wrapped) — for the common
no-space value this is byte-identical to plain `_mask`, so existing
keyword-assignment output is unchanged.

Group 1 = keyword (preserved verbatim).
Group 2 = the secret value to redact.
"""

_BEARER_HEADER = re.compile(r"(?i)\bBearer\s+((?:[^\s\"']+|\"(?:\\.|[^\"\\])*\"?|'[^']*'?)+)")
"""Matches the bare HTTP `Authorization: Bearer <token>` form where
keyword and token are separated by whitespace rather than `:` / `=`.
This is the shape that flows into ``BackendError.__str__`` when an
SDK exception message embeds an upstream HTTP header verbatim — the
``_KEYWORD_ASSIGN`` regex's required ``[:=]`` separator does not cover
it. The token is masked while the literal word ``Bearer`` is preserved
to keep the redacted output recognizable as an auth header.

Group 1 is the same quote-aware shell-word fragment as `_KEYWORD_ASSIGN`
/ `_SHELL_WORD` (masked via `_mask_glued_value`): a bare `(\\S+)` truncates
a quoted value at the in-quote space (`Bearer "a b"` -> masks only `"a`,
leaks `b"`). A real base64url bearer token has no space, so its output is
byte-identical to the old `\\S+` form.

Group 1 = the token to redact.
"""

_SENSITIVE_KEY_NAMES = re.compile(r"(?i)(password|secret|token|api[_-]?key|bearer)")
"""Matches a dict key name that, by itself, signals the associated
value is sensitive. Used by structured-data walkers to mask values
whose adjacent key is one of these keywords (e.g. JSON-like
``{"password": "..."}`` where the value alone does not match
`_KEYWORD_ASSIGN`)."""


def is_sensitive_key(key: str) -> bool:
    """Return True if `key` looks like a secret-bearing field name.

    Helpers that walk dict-like structures use this to decide whether to
    mask the whole adjacent value regardless of its content.
    """
    return _SENSITIVE_KEY_NAMES.search(key) is not None


_JWT = re.compile(r"eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+")
"""Three-segment base64url JWT (header.payload.signature)."""

_SK_KEY = re.compile(r"sk-[a-zA-Z0-9-]{20,}")
"""Anthropic / OpenAI `sk-...` API key prefix."""


_URL_USERINFO = re.compile(r"(?i)([A-Za-z][A-Za-z0-9+.-]{0,30}://[^:@/\s]+):([^@/\s]+)@")
"""Two-segment URL userinfo `scheme://user:<password>@host`.

Scheme match is case-insensitive (RFC 3986: scheme is case-independent),
so `HTTPS://` / `REDIS://` fire too. The userinfo run forbids `/` so a
path like `host/a:b@c` is not mistaken for userinfo. The scheme run is
length-bounded (`{0,30}` — real schemes are short) so a long non-URL
alphanumeric blob (cert dump / base64 / log line, which flow through this
hot-path function) cannot trigger O(n^2) backtracking at every start
position.

Group 1 = `scheme://user` prefix (preserved verbatim).
Group 2 = the password to redact.
"""

_URL_TOKEN = re.compile(r"(?i)([A-Za-z][A-Za-z0-9+.-]{0,30}://)([^:@/\s]+)@")
"""Single-segment URL userinfo `scheme://<token>@host` (no colon, token
directly before `@`), covering PAT-embedded clone URLs such as
`https://ghp_xxx@host`. A pure-username URL (`ssh://deploy@host`) is
over-masked here — accepted as a security-side trade-off.

Group 1 = `scheme://` prefix (preserved verbatim).
Group 2 = the token to redact.
"""

_ENV_CREDENTIAL = re.compile(
    r"\b(PGPASSWORD|MYSQL_PWD|REDIS_PASSWORD|REDISCLI_AUTH|MONGODB_PASSWORD)="
    r"((?:[^\s\"']+|\"(?:\\.|[^\"\\])*\"?|'[^']*'?)+)"
)
"""Exact-name, `=`-anchored credential env assignments missed by the
`\\b(password|...)` word-boundary keyword rule (`PGPASSWORD`, `MYSQL_PWD`).
The `=` anchor naturally excludes `MYSQL_PASSWORD_FILE=/path` (the `=`
follows `_FILE`, not a whitelisted name) and `PWD=/home/x` (`PWD` is not
whitelisted), so no `_FILE` lookahead is needed.

Group 1 = the env name (preserved verbatim).
Group 2 = the secret value to redact.
"""


# A shell word = one-or-more concatenated runs of (non-space-non-quote |
# double-quoted span | single-quoted span). The concatenation `(?:...)+`
# is what makes `-p"my secret"` (glued quote with an inner space) a single
# token, plugging the bare-`\S+` leak that truncates at the in-quote space.
# The double-quoted alt consumes `\.` so an escaped quote (`"a\"b"`, common in
# curl JSON payloads) does not falsely close the span. Single quotes take no
# escape in shell, so the content needs no escape handling.
# The closing quote is OPTIONAL (`"...?`): one alternative covers both a closed
# quote and an UNTERMINATED one (no close before EOS), so a secret in
# `mysql -p"oops-no-close` is masked rather than leaked. Using a single
# optional-close alternative — instead of a separate closed + unterminated pair
# that BOTH start with `"` — is what keeps tokenization LINEAR: an overlapping
# pair rescans a long escaped-quote run (`"` + `\"`*n, e.g. a truncated curl JSON
# body) at every start position, an O(n^2) ReDoS on this hot-path function.
_SHELL_WORD = re.compile(r"(?:[^\s\"']+|\"(?:\\.|[^\"\\])*\"?|'[^']*'?)+")

# Long flags whose next token is the secret value (A, tool-agnostic).
# Compared via casefold so `--Token` matches.
_LONG_FLAG_NAMES = frozenset({"--password", "--secret", "--token", "--api-key", "--api_key"})

# Command-head wrappers that are transparently skipped to find the real
# command token (C decision 3). `docker exec` is handled specially.
_WRAPPER_NAMES = frozenset({"sudo", "env", "nice", "time", "ssh", "docker"})

# Per-wrapper options that take a following value (so the value token is
# skipped too when finding the real command). Options not listed are
# treated as value-less; an unlisted value-taking option is a best-effort
# residual (its value may be mistaken for the command -> that segment is
# left unredacted, safe-side).
_WRAPPER_VALUE_OPTS: dict[str, frozenset[str]] = {
    "sudo": frozenset(
        {
            "-u",
            "-g",
            "-p",
            "-C",
            "-U",
            "-h",
            "-r",
            "-t",
            "-R",
            "-T",
            "--user",
            "--group",
            "--prompt",
            "--chdir",
            "--chroot",
            "--command-timeout",
            "--type",
            "--role",
            "--close-from",
            "--host",
        }
    ),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "docker": frozenset(
        {"-u", "--user", "-e", "--env", "-w", "--workdir", "--detach-keys", "--env-file"}
    ),
    "ssh": frozenset(
        {
            "-p",
            "-i",
            "-o",
            "-l",
            "-b",
            "-B",
            "-c",
            "-D",
            "-E",
            "-e",
            "-F",
            "-I",
            "-J",
            "-L",
            "-m",
            "-O",
            "-Q",
            "-R",
            "-S",
            "-W",
            "-w",
        }
    ),
    "nice": frozenset({"-n", "--adjustment"}),
    "time": frozenset({"-o", "-f", "--output", "--format"}),
}


def _skip_wrapper_opts(
    tokens: list[tuple[str, int, int]], i: int, value_opts: frozenset[str]
) -> int | None:
    """Skip leading `-`-prefixed options after a wrapper name, consuming the
    value of any value-taking option. Returns the new index, or None if a
    value-taking option is missing its value (truncated -> safe-side)."""
    while i < len(tokens) and tokens[i][0].startswith("-"):
        opt = tokens[i][0]
        i += 1
        if opt in value_opts:
            if i >= len(tokens):
                return None
            i += 1
    return i


def _mask(value: str) -> str:
    """Replace `value` with `<first4>...<last4>` (or `****` if too short)."""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


def _segment_commands(s: str) -> list[tuple[str, int, int]]:
    """Coarsely split `s` into suspected command segments, quote-aware.

    Splits on `;` / `|` / `&&` / `||` / a lone `&` / newline that appear
    OUTSIDE single/double quotes, keeping each segment's source span. A
    separator inside a quoted span (e.g. `&` in a `?a=1&b=2` URL argument)
    does not split, so the command head of that segment is not misread.
    """
    segments: list[tuple[str, int, int]] = []
    cursor = 0
    i = 0
    n = len(s)
    quote: str | None = None
    while i < n:
        c = s[i]
        if quote is not None:
            if quote == '"' and c == "\\":
                i += 2  # escaped char inside a double quote (`\"` does not close)
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ('"', "'"):
            quote = c
            i += 1
            continue
        if c in (";", "|", "&", "\n"):
            seplen = 2 if (c in "&|" and i + 1 < n and s[i + 1] == c) else 1
            segments.append((s[cursor:i], cursor, i))
            i += seplen
            cursor = i
            continue
        i += 1
    segments.append((s[cursor:], cursor, n))
    return segments


def _shell_tokens(segment: str) -> list[tuple[str, int, int]]:
    """Tokenize `segment` into shell words with source spans.

    Each token is `(raw_text, start, end)` where the span indexes into
    `segment`. Concatenated quote runs collapse into one token
    (`-p"my secret"` is a single token). Never raises: an unterminated
    quote leaves the run stopped before the quote.
    """
    return [(m.group(0), m.start(), m.end()) for m in _SHELL_WORD.finditer(segment)]


def _command_head(tokens: list[tuple[str, int, int]]) -> str | None:
    """Return the real command token after skipping wrapper prefixes.

    Skips `sudo` / `env` (with `KEY=VALUE`) / `docker exec <container>` /
    `ssh <host>` / `nice` / `time`, consuming each wrapper's leading options
    (value-taking options also consume their value, per `_WRAPPER_VALUE_OPTS`).
    Returns `None` when a wrapper option/host/container cannot be resolved —
    the safe side: the segment is left untouched.
    """
    i = 0
    n = len(tokens)
    while i < n:
        word = tokens[i][0]
        if word in {"sudo", "env", "nice", "time"}:
            nxt = _skip_wrapper_opts(tokens, i + 1, _WRAPPER_VALUE_OPTS[word])
            if nxt is None:
                return None
            i = nxt
            if word == "env":
                while i < n and "=" in tokens[i][0] and not tokens[i][0].startswith("-"):
                    i += 1
            continue
        if word == "docker":
            if i + 1 < n and tokens[i + 1][0] == "exec":
                nxt = _skip_wrapper_opts(tokens, i + 2, _WRAPPER_VALUE_OPTS["docker"])
                if nxt is None or nxt >= n:
                    return None
                i = nxt + 1  # skip the container name
                continue
            return word
        if word == "ssh":
            nxt = _skip_wrapper_opts(tokens, i + 1, _WRAPPER_VALUE_OPTS["ssh"])
            if nxt is None or nxt >= n:
                return None
            i = nxt + 1  # skip the host
            continue
        return word
    return None


def _shell_true_value(raw: str) -> str:
    """Return the shell-concatenated value of `raw` with all quote chars
    stripped (`"my secret"` -> `my secret`, `"sec"tail` -> `sectail`)."""
    return raw.replace('"', "").replace("'", "")


def _mask_glued_value(raw: str) -> str:
    """Mask a flag value token, preserving single-shell-word shape.

    Takes the shell-true value (all quotes stripped), masks it, and wraps the
    result in one pair of double quotes only when it contains whitespace — so
    a re-tokenization of the output yields the same single token (idempotent).
    """
    masked = _mask(_shell_true_value(raw))
    if any(c.isspace() for c in masked):
        return f'"{masked}"'
    return masked


def _redact_command_credentials(segment: str) -> str:
    """Redact known flag-form credentials within a single command segment.

    Identifies credential tokens via shell tokenization + a command-head
    whitelist, then rewrites only those tokens' source spans in place. Other
    tokens and all separators are preserved byte-for-byte.
    """
    tokens = _shell_tokens(segment)
    if not tokens:
        return segment

    head = _command_head(tokens)

    # Collect `(start, end, replacement)` edits, then apply right-to-left so
    # earlier spans keep their offsets.
    edits: list[tuple[int, int, str]] = []

    for idx, (raw, start, _end) in enumerate(tokens):
        # [A] long flag: next token is the value (unless it is another flag).
        if raw.casefold() in _LONG_FLAG_NAMES:
            if idx + 1 < len(tokens):
                nxt_raw, nxt_start, nxt_end = tokens[idx + 1]
                if not nxt_raw.startswith("-"):
                    edits.append((nxt_start, nxt_end, _mask_glued_value(nxt_raw)))
            continue

        # [B] tool-specific short flags, only when the head is whitelisted.
        if head is None:
            continue

        glued = _glued_flag_edit(head, raw, start)
        if glued is not None:
            edits.append(glued)
            continue

        spaced = _spaced_flag_edit(head, raw, idx, tokens)
        if spaced is not None:
            edits.append(spaced)

    out = segment
    for start, end, replacement in sorted(edits, reverse=True):
        out = out[:start] + replacement + out[end:]
    return out


def _glued_flag_edit(head: str, raw: str, start: int) -> tuple[int, int, str] | None:
    """Return an in-span edit for a suffix-glued credential flag, or None.

    The replacement is confined to the value region after the flag letters
    inside this token's own span, so a glued value equal to the command head
    (`mysql -pmysql`) never corrupts the head.
    """
    prefixes: tuple[str, ...]
    if head in {"mysql", "mariadb"}:
        prefixes = ("-p",)
    elif head == "redis-cli":
        prefixes = ("-a",)
    elif head in {"mongosh", "mongo", "sshpass"}:
        prefixes = ("-p",)
    else:
        return None

    for prefix in prefixes:
        if raw.startswith(prefix) and len(raw) > len(prefix):
            value_start = start + len(prefix)
            value_raw = raw[len(prefix) :]
            return (value_start, start + len(raw), _mask_glued_value(value_raw))
    return None


def _spaced_flag_edit(
    head: str,
    raw: str,
    idx: int,
    tokens: list[tuple[str, int, int]],
) -> tuple[int, int, str] | None:
    """Return an in-span edit for a space-separated tool credential, or None.

    Covers `redis-cli -a <v>` / `--pass <v>`, `mongosh -p <v>`,
    `sshpass -p <v>`, and `curl -u user:<v>` / `--user user:<v>`.
    """

    def _next_value() -> tuple[int, int, str] | None:
        if idx + 1 >= len(tokens):
            return None
        nxt_raw, nxt_start, nxt_end = tokens[idx + 1]
        if nxt_raw.startswith("-"):
            return None
        return (nxt_start, nxt_end, _mask_glued_value(nxt_raw))

    if head == "redis-cli" and raw in {"-a", "--pass"}:
        return _next_value()
    if head in {"mongosh", "mongo"} and raw == "-p":
        return _next_value()
    if head == "sshpass" and raw == "-p":
        return _next_value()
    if head == "curl" and raw in {"-u", "--user"}:
        if idx + 1 >= len(tokens):
            return None
        nxt_raw, nxt_start, nxt_end = tokens[idx + 1]
        if nxt_raw.startswith("-"):
            return None
        user, sep, password = _shell_true_value(nxt_raw).partition(":")
        if not sep:
            return None  # pure username, no `:` — leave untouched
        masked = _mask_glued_value(password)
        return (nxt_start, nxt_end, f"{user}:{masked}")
    return None


def redact_text(s: str) -> str:
    """Return a redacted copy of `s` with default secret patterns masked.

    The function is order-sensitive: keyword-assignment matches are
    handled first so that values containing `sk-...` or JWT fragments
    inside an assignment are masked once (avoiding double-replacement
    that would corrupt the kept-prefix marker).
    """

    def _sub_assign(match: re.Match[str]) -> str:
        keyword = match.group(1)
        value = match.group(2)
        return f"{keyword}={_mask_glued_value(value)}"

    out = _KEYWORD_ASSIGN.sub(_sub_assign, s)
    out = _BEARER_HEADER.sub(lambda m: f"Bearer {_mask_glued_value(m.group(1))}", out)
    out = _JWT.sub(lambda m: _mask(m.group(0)), out)
    out = _SK_KEY.sub(lambda m: _mask(m.group(0)), out)

    # C — URL userinfo (standalone, not token-based). Two-segment form first
    # so `scheme://user:pw@` masks the password; the single-segment form then
    # covers `scheme://token@`.
    out = _URL_USERINFO.sub(lambda m: f"{m.group(1)}:{_mask(m.group(2))}@", out)
    out = _URL_TOKEN.sub(lambda m: f"{m.group(1)}{_mask(m.group(2))}@", out)

    # D — exact-name credential env assignments.
    out = _ENV_CREDENTIAL.sub(lambda m: f"{m.group(1)}={_mask_glued_value(m.group(2))}", out)

    # A/B — flag-form credentials, per command segment, written back in place.
    segments = _segment_commands(out)
    if len(segments) == 1:
        return _redact_command_credentials(out)
    pieces: list[str] = []
    prev_end = 0
    for text, start, end in segments:
        pieces.append(out[prev_end:start])  # the separator before this segment
        pieces.append(_redact_command_credentials(text))
        prev_end = end
    pieces.append(out[prev_end:])
    return "".join(pieces)


# Cassette commit gate uses a broader standard than runtime log redaction:
# a cassette is committed to git and reviewed by a human, so it is held to a
# higher bar than runtime log output (where `redact_text` only scrubs the
# most obvious leaks while deliberately keeping HOME / paths that aid
# debugging). Both `cassette_lint.py` and `RecordingBackend` import this
# single source so "recorded then linted" stays consistent. Each tuple is
# ``(name, compiled_regex)``; ``name`` is reported on a hit so a reviewer can
# identify the firing rule without re-scanning.
CASSETTE_SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # API-key prefixes (Anthropic / OpenAI ``sk-...``).
    ("anthropic_or_openai_sk_key", re.compile(r"sk-[A-Za-z0-9_-]{6,}")),
    # ``Authorization: Bearer <token>``.
    ("bearer_token", re.compile(r"(?i)\bBearer\s+\S+")),
    # Three-segment JWT (header.payload.signature).
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+")),
    # ``password=...`` / ``api_key=...`` / ``token=...`` / ``secret=...``
    # assignment forms. The trailing value must be at least two chars to
    # avoid matching empty-value JSON-encoded ``"password":""`` keys that
    # cassettes occasionally need to express literal empty-string fields.
    (
        "credential_assignment",
        re.compile(r"(?i)\b(password|secret|token|api[_-]?key)\s*[:=]\s*\S{2,}"),
    ),
    # User home directories — macOS ``/Users/<name>`` and Linux ``/home/<name>``.
    ("user_home_path", re.compile(r"/(Users|home)/[A-Za-z0-9._-]+")),
    # ``.ssh`` directories, anywhere in the path.
    ("ssh_path", re.compile(r"\.ssh(/|\\)")),
    # IPv4 literals (not 0.0.0.0 / 127.0.0.1 ish — block both private and
    # public; cassettes have no business holding any specific IP).
    ("ipv4_address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    # Email addresses.
    ("email_address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # Hostnames / FQDNs with at least one label + a common TLD or environment
    # suffix. Catches strings like ``prod-db.internal.example.com`` or
    # ``auth.corp.local`` that may leak via inspector output into cassettes.
    # The suffix set is narrow on purpose: a generic ``\.[a-z]{2,}`` would
    # collide with model IDs ("claude-opus-4-7"), tool names, and other
    # legitimate dotted tokens we expect inside cassette bodies.
    (
        "hostname_or_fqdn",
        re.compile(
            r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
            r"(?:internal|intranet|local|lan|corp|company|enterprise|prod|production"
            r"|staging|dev|test|example|home|office|com|net|org|io|app|cloud|tech)\b",
            re.IGNORECASE,
        ),
    ),
)


def detect_sensitive_text(text: str) -> str | None:
    """Return the name of the first `CASSETTE_SENSITIVE_PATTERNS` rule that
    matches `text`, or `None` if none match.

    This is the cassette commit gate's detector. It differs from
    `redact_text` in two ways:

    - **Detection vs masking**: this returns a rule name (or None) so a caller
      can fail-and-reject; `redact_text` rewrites the string in place to mask
      secrets and is used at runtime rendering boundaries.
    - **Wider standard**: cassettes are committed to git, so this gate flags
      categories runtime redaction deliberately keeps (HOME / `.ssh` paths,
      IPv4, email, hostname-FQDN) to aid debugging. `redact_text`'s runtime
      masking semantics are intentionally narrower and are not changed here.
    """
    for name, pattern in CASSETTE_SENSITIVE_PATTERNS:
        if pattern.search(text):
            return name
    return None
