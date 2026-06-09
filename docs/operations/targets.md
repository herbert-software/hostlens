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

  - name: app-container
    type: docker
    enabled: true
    display_name: "App Container"
    description: "Read-only inspection of an existing running container"
    tags: [docker, app]
    container: hostlens-app             # container name or id (required)
    # docker_host is optional; omit to use the default local socket
    # (unix:///var/run/docker.sock). Only local unix:// sockets are
    # accepted — see "Docker targets" below.
    docker_host: unix:///var/run/docker.sock
```

### Field reference

**Common (all target types):**

| field | type | default | notes |
|---|---|---|---|
| `name` | str | — | must match `^[a-z][a-z0-9_\-]{0,63}$`; enforced at load + constructor + register |
| `type` | `"local"` / `"ssh"` / `"docker"` | — | discriminator |
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

**Docker-only:**

| field | type | default | notes |
|---|---|---|---|
| `container` | str | — | required, non-empty; container name or id of an **existing** container to inspect |
| `docker_host` | str \| null | null | optional docker endpoint; only local `unix://` sockets are accepted (see "Docker targets"). Omit to use docker-py's `from_env()` default (typically `unix:///var/run/docker.sock`) |

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

Service inspectors (those reaching an external service such as Redis /
MySQL / PostgreSQL via a client CLI) declare their connection secret with
the `HOSTLENS_` prefix (e.g. `HOSTLENS_REDIS_PASSWORD`,
`HOSTLENS_MYSQL_PWD`) and remap it inside the collector to the client's
native env auth channel (`REDISCLI_AUTH` / `MYSQL_PWD` / `PGPASSWORD`),
so the password never reaches `argv` (never `-a <pwd>` / `-p<pwd>`).
Running such an inspector against a **password-protected** backend over
SSH therefore **requires** the remote sshd to be configured with
`AcceptEnv HOSTLENS_*`; if it is not, the non-empty secret is dropped at
the allowlist and the inspector honestly fails as `status=exception`
(auth failure) rather than silently reporting a healthy backend. Against
a **no-auth** backend the inspector still succeeds — the empty secret
carries nothing the allowlist could drop. Note that pre-spike seed
inspectors (`redis.slowlog`, `postgres.bloat_tables`) still declare
non-`HOSTLENS_` secret names pending migration; the `HOSTLENS_*`
guarantee above covers the service-inspector-contract probes and every
inspector authored after them.

## Docker targets

A `type: docker` target runs Inspector commands inside an **already
existing** container via docker-py (`container.exec_run` for `exec`,
`container.get_archive` for `read_file`). It performs **only read-only**
operations — no container lifecycle management (create / start / stop /
restart / rm).

### Installation

docker-py is an optional dependency. Install the extra before using a
docker target:

```
pip install "hostlens[docker]"
```

Without it, constructing or probing a docker target raises
`TargetError(kind="docker_sdk_unavailable")` carrying the same install
hint (it never lets a bare `ImportError` escape).

### `docker_host` is local-socket only

`docker_host` accepts **only** a non-empty local unix socket of the form
`unix:///path/to/docker.sock` (lowercase `unix://`). Everything else is
rejected at config load time with
`ConfigError(kind="docker_host_remote_not_supported")`:

- remote schemes (`tcp://`, `ssh://`, `http(s)://`, `npipe://`),
- bare paths without a scheme (`/var/run/docker.sock`),
- an empty `unix://`,
- case mismatches (`UNIX://...`),
- relative socket paths (`unix://foo`).

Remote docker over TCP + TLS (and its credential loading) is a deliberate
non-goal of this milestone; the field is reserved for a follow-up. Omitting
`docker_host` uses docker-py's `from_env()` default.

### Security: docker socket access is host-root-equivalent

Access to the docker socket is **equivalent to root on the host** —
anyone who can talk to the daemon can start a privileged container that
mounts the host filesystem. Treat a `targets.yaml` containing a docker
target with the same gravity as root credentials:

- Do **not** expose `targets.yaml` to untrusted users.
- Restrict file permissions on `targets.yaml` and the docker socket to
  the operator running Hostlens.

Hostlens does **not** add a doctor check for this (the docker `targets`
probe stays type-agnostic); the risk is inherent to the docker model, not
a Hostlens defect, so it is documented here rather than enforced at
runtime. The default socket path `unix:///var/run/docker.sock` is a public,
non-secret path and is not redacted from error messages — `docker_host` is
already constrained to local sockets, so docker errors never carry a remote
endpoint credential.

### Environment injection (no `AcceptEnv` filtering)

Unlike SSH targets, a docker target injects `env` via
`exec_run(environment=...)`, which reaches the container process
environment directly — it is **not** subject to the remote sshd
`AcceptEnv` allowlist that filters SSH `env=`. So the `HOSTLENS_*`-prefix
workaround required for SSH (see "SSH remote `AcceptEnv` configuration"
above) is unnecessary for docker targets: any env var name passes through.
As with SSH, secrets are passed only through the `environment=` parameter
and are never spliced into the command string (no `export VAR=value; cmd`),
so they never appear in the container `ps` output or shell history.

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
  only, no password), `none` (local / docker targets — they carry no SSH
  credentials)
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
