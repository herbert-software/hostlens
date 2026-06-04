# `hostlens notify` Operations Guide

M5 lands the Notifier layer: rendered inspection reports are pushed to
Telegram / 飞书 (Lark) channels on a schedule, and the `hostlens notify`
CLI lets operators introspect channels, dry-run renders, and send a real
test ping. This document covers the channel config file, the `only_if`
routing expression, the three CLI subcommands, the `doctor`
`--check-channels` probe, and the no-token local Demo Path.

> Reliability parameters (concurrency / timeout / retry / truncation) live
> in [OPERABILITY.md §8](../OPERABILITY.md#8-notifier-可靠性合约). This guide
> is the user-facing how-to; OPERABILITY is the limits SOT.

## Channel configuration (`notifiers.yaml`)

Channels are configured in `~/.config/hostlens/notifiers.yaml` (path from
`Settings.notifiers_config_path`; overridable via settings). The top level
is a `channels:` mapping of `<instance name> → { type, ...fields }`. The
`type` selects the adapter (`telegram` / `lark`); the remaining fields are
the adapter's config.

```yaml
channels:
  ops-telegram:
    type: telegram
    bot_token: ${TELEGRAM_BOT_TOKEN}
    chat_id: ${TELEGRAM_CHAT_ID}
  ops-lark:
    type: lark
    webhook_url: ${LARK_WEBHOOK_URL}
    secret: ${LARK_SIGN_SECRET}        # optional; enables HMAC sign
```

Per-type fields:

| type | required | optional |
|---|---|---|
| `telegram` | `bot_token`, `chat_id` | — |
| `lark` | `webhook_url` | `secret` (enables HMAC-SHA256 signing) |

### Secrets via `${ENV_VAR}` injection

Field values may embed `${ENV_VAR}` placeholders, resolved from the
environment at load time. **Never** write a bot token / webhook URL / sign
secret in plaintext into `notifiers.yaml` or commit it — use `${ENV_VAR}`
(CLAUDE.md §7).

Injection is fail-loud and single-layer:

- `${VAR}` → `os.environ["VAR"]`; an **unset** variable raises a
  `ConfigError` naming it (never silently resolves to `""`).
- `${}` (empty name) is illegal and raises.
- A bare `$` or a malformed `${X` (no closing brace) does not match the
  placeholder pattern and is kept verbatim.
- A substituted value is not re-scanned, so injected content that happens
  to contain `${...}` is left untouched.

After expansion each channel's `validate_config` must pass — required
fields must be **present and non-empty** (an empty string counts as
missing).

## Routing: `notify` + `only_if`

A schedule manifest references channels and gates each one with an
optional `only_if` expression:

```yaml
# schedules/nightly-cpu.yaml fragment
notify:
  - channel: ops-telegram
    only_if: "severity >= warning"      # only warning/critical get pushed
  - channel: ops-lark
    only_if: "'disk_full' in tags"      # only when a finding carries this tag
```

Each entry is `{ channel, only_if }`. `only_if` is optional — omit it to
always send. The empty string `""` is **not** "always send"; it is an
illegal value that fails loud at load time (to always send, omit the
field).

### `only_if` expression language

`only_if` reuses the hardened inspector finding DSL (`inspectors.dsl`) —
the same static-AST-gated, timeout-bounded evaluator, not a raw `eval`.
The routing context binds:

- `severity` — the report's aggregate severity as an **ordered rank**
  (`info=0 < warning=1 < critical=2`). The aggregate is the max over all
  finding severities (a report with no findings derives `info`).
- `info` / `warning` / `critical` — the three name→rank bindings, so
  `severity >= warning` is a numeric comparison (not lexicographic).
- `tags` — the sorted union of every finding's `tags`, so `'x' in tags`
  works.

Examples:

| expression | sends when |
|---|---|
| `severity >= warning` | aggregate severity is warning or critical |
| `severity == critical` | aggregate severity is exactly critical |
| `'disk_full' in tags` | any finding tagged `disk_full` |
| `severity >= warning and 'oom' in tags` | both conditions hold |

Allowed constructs are whatever the inspector DSL permits (comparisons,
boolean ops, membership, and its whitelisted functions); forbidden
constructs (lambda / comprehension / `__import__` / dunder attribute /
import) are rejected by the AST gate.

**Two validation timings** (a deliberate split):

- **Load time** — the manifest loader runs every `only_if` through the
  DSL's `validate_ast`. A malformed / forbidden / empty-string expression
  fails loud *before* the scheduler ever fires. This is a syntax/AST gate;
  it does **not** resolve whether names exist, so a typo like `severty`
  passes here.
- **Run time** — the expression is evaluated against the report context.
  **Any** evaluation exception (undefined name from a typo, type mismatch,
  timeout, every `simpleeval` runtime class) is caught and recorded as a
  `NotifyResult(status="failed")` for that channel — it never bubbles out
  of notify dispatch, never changes the already-decided `RunStatus`, and
  never disturbs the other channels.

A falsy `only_if` result is a normal routing **skip**
(`NotifyResult(status="skipped")`), distinct from a failure.

## CLI

### `hostlens notify channels [--json]`

List every configured channel with its type and config-validation status
(does `validate_config` pass, are referenced env vars set). **Read-only**:
never sends, never prints a secret value.

A missing / unreadable / malformed `notifiers.yaml` produces a readable
message and a non-crash exit (empty list for `--json`, a hint line
otherwise) — never a Python traceback. Per-channel problems (unknown type,
unset env var, failed validation) are surfaced as `valid=false` rows with
a reason, so one bad channel does not hide the healthy ones.

### `hostlens notify render --report <id> --channel <name> [--only-if <expr>]`

Load a persisted Report (by `report_id`, from `hostlens reports list`),
render the target channel's native payload (Telegram MarkdownV2 text /
Lark card JSON), and write it to stdout. **Dry-run is the only behavior:
nothing is ever sent.** This is the no-token, no-network Demo Path.

- `--only-if <expr>` (optional) prints the routing decision
  (send / skip / failed + reason) to stderr without sending; stdout stays
  the rendered payload.
- A truncated payload prints a `note: payload was truncated ...` line to
  stderr.
- Unknown `report_id` / orphan-stored report / unknown channel all fail
  loud with a non-zero exit and a readable reason.

### `hostlens notify test --channel <name> [--yes]`

Really send one fixed ping message to the channel (no Report needed). As
an **outbound op**:

- a non-TTY run without `--yes` exits 1 (never sends);
- a TTY run confirms interactively.

Per the spec's audited exemption, `notify test` does **not** trigger the
global write-op EUID==0 root refusal — it creates no file and changes no
inspected-host state, it only makes one outbound HTTPS request. (A future
CLI that *writes* `notifiers.yaml` would fall under §4.5 and must refuse
root.)

### Exit codes

Project-wide `3 > 2 > 1 > 0`:

- `0` success.
- `1` business failure — unknown report / orphan / unknown channel for
  `render`; a `test` send that did not succeed; the non-TTY-no-`--yes`
  guard for `test`.
- `2` configuration error — a present-but-malformed `notifiers.yaml` for
  `render` / `test`.
- `3` usage error — missing / invalid options.

stdout carries machine output (channel list / rendered payload); stderr
carries hints and errors; no traceback ever reaches the user.

## `doctor --check-channels`

`hostlens doctor --check-channels` adds a lightweight connectivity / config
probe per configured channel, landed under `doctor --json`
`checks.channels`:

- **Telegram** — calls the Bot API `getMe` (read-only; never delivers a
  message).
- **Lark** — validates config completeness only (does not post a business
  message).

A failing probe (invalid token / missing env var / failed validation) is
marked red with a reason but does **not** affect the other doctor checks.
No `notifiers.yaml` → `status="ok"` ("no channels configured").

## Demo Path (no token, no network)

Render a persisted report to a channel's native payload, entirely offline:

1. Pick a persisted report id: `hostlens reports list <target>` (or run
   `hostlens demo` to generate one).
2. Create a minimal `~/.config/hostlens/notifiers.yaml` (the Telegram /
   Lark examples above; the token values can be any non-empty string for a
   pure render — `render` never authenticates because it never sends).
   Export the referenced env vars so `${ENV_VAR}` injection resolves.
3. `hostlens notify channels` — confirm the channel shows up `valid=true`.
4. `hostlens notify render --report <id> --channel ops-telegram --only-if "severity >= warning"`
   — the rendered MarkdownV2 (or Lark card JSON) goes to stdout; the
   routing decision goes to stderr. Nothing is sent.

Optional real smoke test (needs a real bot/webhook): set the real
secrets and run
`hostlens notify test --channel ops-telegram --yes` to deliver one ping to
your own test chat.

## Known accepted risks

- **At-most-3-attempt, at-least-once delivery**: a send is retried up to 3
  times with bounded backoff; there is no de-dup, so a retry can deliver a
  duplicate message (accepted). See OPERABILITY §8.
- **No dead-letter queue**: a channel that exhausts its retry budget is
  recorded as `NotifyResult(status="failed", error=...)` in the Run; there
  is no persistent re-delivery queue in M5 (deferred).
- **Oversized payloads are truncated**, not split: a body over the
  channel's length limit (Telegram 4096 code units / Lark card body limit)
  is clipped at a safe boundary and flagged `truncated=True`.
