# Targets Operations Guide

How to configure and operate `ExecutionTarget` instances in Hostlens.

## targets.yaml location

Default path: `~/.config/hostlens/targets.yaml` (overridable via
`HOSTLENS_TARGETS_CONFIG_PATH` env var, see `Settings.targets_config_path`).

## Configuration example

```yaml
version: "1"
targets:
  - name: my-local
    type: local
    enabled: true
    display_name: "Local Dev Box"
    description: "Loopback subprocess target for testing"
    tags: [dev, sandbox]

  - name: prod-web
    type: ssh
    enabled: true
    display_name: "Production Web Server"
    description: "Front-end web tier"
    tags: [prod, web]
    host: web.example.com
    user: hostlens
    port: 22
    key_path: ~/.ssh/hostlens_id_ed25519
    # Password / passphrase MUST use ${VAR} placeholders. Inline plaintext
    # works but doctor will warn — see "Credential best practices" below.
    password: ${HOSTLENS_PROD_WEB_PASSWORD}
    passphrase: ${HOSTLENS_PROD_WEB_PASSPHRASE}
    connect_timeout: 10

  - name: maint-db
    type: ssh
    enabled: false                       # disabled; doctor marks 'skipped'
    host: db.example.com
    user: hostlens
    key_path: ~/.ssh/hostlens_id_ed25519
```

### Field reference

**Common (all target types):**

| field | type | default | notes |
|---|---|---|---|
| `name` | str | — | must match `^[a-z][a-z0-9_\-]{0,63}$`; enforced at load + constructor + register |
| `type` | `"local"` / `"ssh"` | — | discriminator |
| `enabled` | bool | `true` | when `false`, `exec` / `read_file` raise `TargetError(kind="target_disabled")` without connecting; doctor marks `connectivity: "skipped"`; `list_targets` ToolSpec filters them out unless `include_disabled=true` |
| `display_name` | str \| null | null | human-friendly label; only surfaced through `list_targets` projection |
| `description` | str \| null | null | free text; surfaced through `list_targets` |
| `tags` | list[str] | `[]` | surfaced through `list_targets` |

**SSH-only:**

| field | type | default | notes |
|---|---|---|---|
| `host` | str | — | required |
| `user` | str | — | required |
| `port` | int | `22` | |
| `key_path` | str \| null | null | path to private key; path itself is not a secret, file contents are loaded by asyncssh |
| `password` | str \| null | null | `${VAR}` placeholder strongly recommended |
| `passphrase` | str \| null | null | `${VAR}` placeholder strongly recommended |
| `connect_timeout` | int \| null | null | seconds; defaults to 10 if null |

## Credential best practices

1. **Always use `${ENV_VAR}` placeholders** for `password` and `passphrase`.
   Inline plaintext is accepted (loader does not reject it), but
   `hostlens doctor` will emit a warning with `credential_source:
   "inline_plaintext"`.
2. Placeholders are **only** allowed on `password` / `passphrase` fields.
   Putting `${VAR}` on `host` / `user` / `port` / `key_path` raises
   `ConfigError(kind="env_placeholder_not_allowed_here")`.
3. Missing env vars raise
   `ConfigError(kind="missing_env_var", var_name=..., target=...)`
   at load time — Hostlens never silently uses an empty password.
4. `repr(SSHEntry)` and `str(SSHEntry)` mask `password` / `passphrase`
   automatically, so `print()` and structlog do not leak secrets.

## SSH remote `AcceptEnv` configuration

OpenSSH's default sshd_config only honors `AcceptEnv LANG LC_*` —
anything else passed via `env=` to `asyncssh.connect` is silently
dropped by the remote. If you need Inspector commands to read host
credentials from env, add this to the remote `/etc/ssh/sshd_config`:

```
AcceptEnv HOSTLENS_*
```

Inspector authors should use the `HOSTLENS_` prefix on env var names so
they pass through this allowlist. As an alternative, pass secrets via
stdin from the Inspector script. **Never** splice them into the
command string (`export VAR=value; cmd`) — that puts the secret into
remote `ps` output and shell history, violating
[`docs/ARCHITECTURE.md` §4](../ARCHITECTURE.md) secret boundary.

## Connection pool behavior (SSH)

Each `SSHTarget` instance holds **one** asyncssh control connection
per process (similar to OpenSSH `ControlMaster auto`):

- **First `exec`** lazily opens the connection.
- **Subsequent `exec`** opens a new SSH channel on the same connection;
  asyncssh supports parallel channels, so concurrent `exec` calls do
  not serialize on a single stdin/stdout.
- **Idle timeout** (`Settings.ssh.idle_timeout_seconds`, default 300s,
  override via `HOSTLENS_SSH__IDLE_TIMEOUT_SECONDS`): the connection
  is closed after this much idle time and reopened on the next exec.
- **Reconnect** is triggered only for `asyncssh.ConnectionLost` /
  `ChannelOpenError` on an already-established connection. Backoff
  sequence is exactly `1s → 4s → 16s` (matches
  [`docs/OPERABILITY.md` §2.2](../OPERABILITY.md)). Exhaustion raises
  `TargetError(kind="ssh_connection_lost", target=name)`.
- **First-connect failures** (`OSError`, `PermissionDenied`, etc.) do
  NOT enter the reconnect loop — they map to dedicated error kinds:
  - `OSError` / `TimeoutError` / `socket.gaierror` / `ConnectionRefused` → `ssh_connect_timeout`
  - `asyncssh.PermissionDenied` / `HostKeyNotVerifiable` / `KeyExchangeFailed` → `ssh_auth_failed` (with three-layer credential scrub)
  - Everything else → `ssh_connect_failed`

## Credential scrubbing (three layers)

When SSH auth fails, the raw `asyncssh` exception string is cleaned
through three layers **in this order** before being wrapped in
`TargetError(kind="ssh_auth_failed")`:

1. **Known-secret exact replace.** SSHTarget looks at its bound
   `TargetEntry.password` / `passphrase` and runs `str.replace(secret,
   "***")` on the message. This guarantees that any password Hostlens
   has actually been configured with cannot leak, regardless of
   whether it happens to match any regex.
2. **`scrub_exception_message`** from
   `hostlens.agent.tools_adapter` — five regex classes covering POSIX
   paths, IPv4 / IPv6, credential key/value pairs, `Bearer` / `sk-…`
   tokens, and `email-at-host` patterns.
3. **Bare credential keyword scrub** — `(?i)(password|passwd|pwd|
   passphrase|secret|token|api[_-]?key|auth)\s+\S+` → `\1 ***`.
   Catches the "key value" (space-separated) form that layer 2 doesn't
   cover, e.g. `with password literal-pwd-do-not-leak`.

This is **safety-biased over-redaction**: text like `password policy`
will be redacted to `password ***`, which is correct because layer 3
cannot tell at the regex level whether the next word is a secret.
Treat this as expected behavior, not a bug. If Hostlens log output
gets flagged this way during local debugging, log the variable in
question through structlog with a key name like `policy_doc` instead
of including the literal token `password`.

## `hostlens doctor --check-targets`

For each configured target, `doctor` reports:

- `connectivity`: `ok` / `failed` / `skipped` (disabled targets are
  always skipped)
- `credential_source`: `env_var` (placeholder expanded), `inline_plaintext`
  (literal in yaml — triggers a warning), `key_only` (SSH key path
  only, no password), `none` (local target)
- `capabilities`: the set probed at last `exec`

If **any** target reports `connectivity: failed`, `doctor` exits 1.
Inline-plaintext password warnings do **not** cause exit 1.

## EUID == 0 (root) policy

Per [`CLAUDE.md` §4.5](../../CLAUDE.md) and the global
"write operations must reject root" rule:

- `hostlens target add` and `hostlens target remove` **refuse to run
  as root** and exit 1 with a remediation message. This prevents
  Hostlens from creating root-owned `targets.yaml` files that an
  unprivileged daemon can't read later.
- `hostlens target list`, `hostlens target test`, and `hostlens doctor`
  are read-only and run fine as root (useful when invoking from a
  root-only systemd unit).
