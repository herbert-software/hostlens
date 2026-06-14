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

  - name: app-pod
    type: k8s
    enabled: true
    display_name: "App Pod"
    description: "Read-only inspection of a Running pod's container"
    tags: [k8s, app]
    pod: my-app-7d9f                    # pod name (required, non-empty)
    namespace: default                  # defaults to "default"
    container: app                      # optional; omit for the pod's first container
    # kubeconfig / context are optional; omit both to use in-cluster auth
    # (when running inside the cluster) or the default kubeconfig.
    kubeconfig: ~/.kube/config
    context: kind-hostlens
```

### Field reference

**Common (all target types):**

| field | type | default | notes |
|---|---|---|---|
| `name` | str | — | must match `^[a-z][a-z0-9_\-]{0,63}$`; enforced at load + constructor + register |
| `type` | `"local"` / `"ssh"` / `"docker"` / `"k8s"` | — | discriminator |
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

**K8s-only:**

| field | type | default | notes |
|---|---|---|---|
| `pod` | str | — | required, non-empty; name of an **existing, Running** pod to inspect |
| `namespace` | str | `"default"` | namespace of the pod |
| `container` | str \| null | null | container name within the pod; omit to target the pod's **first** container (`spec.containers[0]`) |
| `kubeconfig` | str \| null | null | path to a kubeconfig file; omit to use the default kubeconfig or in-cluster auth (see "Kubernetes targets") |
| `context` | str \| null | null | kubeconfig context name; omit to use the kubeconfig's current-context |

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
carries nothing the allowlist could drop. Both former pre-spike seed
inspectors (`redis.slowlog` and `postgres.bloat_tables`) have now
migrated to `HOSTLENS_*` secret names; the grandfather clause is closed
with **no registered grandfathered inspector**, so the `HOSTLENS_*`
guarantee above covers every service-inspector-contract probe and every
inspector authored after them.

> **BREAKING — `redis.slowlog` secret rename.** `redis.slowlog` was one
> of the two grandfathered pre-spike seeds; both it and
> `postgres.bloat_tables` have now migrated to full
> service-inspector-contract compliance, closing the grandfather clause.
> Its
> connection secret env var changed from **`REDIS_PASSWORD`** to
> **`HOSTLENS_REDIS_PASSWORD`** (aligned with the sibling
> `redis.{memory_usage,persistence,replication_lag}` inspectors). The
> collector now remaps it to `REDISCLI_AUTH` so the password never
> reaches `argv`. **Action required:** operators inspecting a
> password-protected Redis must re-export the credential under the new
> name — `export HOSTLENS_REDIS_PASSWORD=...` (the old `REDIS_PASSWORD`
> is no longer read; a stale `REDIS_PASSWORD`-only environment yields
> `status=requires_unmet`, an honest skip rather than a silent pass). On
> SSH targets the new name passes the recommended `AcceptEnv HOSTLENS_*`
> allowlist, so a password-protected Redis is now inspectable over SSH.
> The collector also gained a `-t 5` redis-cli connect timeout (< the 15s
> collect timeout), so a hung connection fails fast and honestly instead
> of stalling to the collect deadline.

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

## Kubernetes targets

A `type: k8s` target runs Inspector commands **inside an already existing,
Running pod's container** via `kubernetes-asyncio`. Like the docker target it
performs **only read-only** operations — no pod lifecycle management (create /
delete / patch / scale / exec-write). It is the analogue of `kubectl exec` for
`exec` and of `kubectl cp` (a `tar`-over-exec stream) for `read_file`.

### Installation

kubernetes-asyncio is an optional dependency. Install the extra before using
a k8s target:

```
pip install "hostlens[k8s]"
```

Without it, using a k8s target raises
`TargetError(kind="k8s_sdk_unavailable")` carrying the same install hint (it
never lets a bare `ImportError` escape). The module still imports without the
extra so the registry / type-checker can reference `KubernetesTarget`.

### Authentication: kubeconfig vs in-cluster

The target builds a per-target client configuration from one of two sources:

- **In-cluster** — when Hostlens runs **inside** the cluster (the
  `KUBERNETES_SERVICE_HOST` env var is set), it uses the mounted service
  account token (`load_incluster_config`). `kubeconfig` / `context` are
  ignored in this mode.
- **Kubeconfig** — otherwise it loads `kubeconfig` (or the default
  `~/.kube/config` when omitted) and selects `context` (or the
  current-context when omitted). All five k8s fields (`pod`, `namespace`,
  `container`, `kubeconfig`, `context`) are **non-secret** path / name values;
  putting a `${VAR}` placeholder on any of them raises
  `ConfigError(kind="env_placeholder_not_allowed_here")` at load time
  (placeholders are reserved for `password` / `passphrase`).

Kubeconfig load failure, an unreachable API server, an auth failure (401),
or missing RBAC (403) all surface as `TargetError(kind="k8s_unavailable")`
with the message scrubbed of any incidental bearer token / home path / IP.

### Required RBAC

The identity Hostlens authenticates as needs, in the target namespace:

- `get` on `pods` (to proactively read the pod's phase + container status
  before any exec — this is how `pod_not_found` / `pod_not_running` /
  `container_not_found` / `container_not_running` are classified
  deterministically rather than from locale-fragile exec error text), and
- `create` on `pods/exec` (the exec websocket backs both `exec` and the
  `tar`-over-exec `read_file`).

A minimal read-only Role:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: hostlens-inspect
  namespace: default
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get"]
  - apiGroups: [""]
    resources: ["pods/exec"]
    verbs: ["create"]
```

Note `pods/exec` is a powerful grant: anyone who can exec into a pod can run
arbitrary commands in that container. Scope the Role to the namespaces /
service account Hostlens actually inspects and treat a `targets.yaml`
containing a k8s target with the same care as cluster credentials.

### Environment injection (over stdin, never in argv)

Unlike the docker target (which injects `env` via `exec_run(environment=...)`,
a dict that never touches a shell), the k8s pod exec API has **no
`environment=` parameter**. The target therefore feeds env over the exec
**stdin** channel as shell-quoted `export <KEY>=<value>` lines followed by the
command and a trailing `exit $?`; the exec `command` is strictly `["/bin/sh"]`.
Consequences:

- Secrets are passed only over stdin and **never appear in the pod's `ps`
  output / process argv** (the integration test asserts this against a live
  pod).
- Because env values go through a shell `export`, each env **key** must be a
  valid shell identifier (`^[A-Za-z_][A-Za-z0-9_]*$`) or the call raises
  `TargetError(kind="invalid_env_key")` — a defense-in-depth guard against
  injection (env keys originate from controlled inspector parameters).
- **Known limitation (asymmetric with docker / ssh / local):** because `cmd`
  itself is fed over stdin, the pod-side command **cannot read external input
  from stdin** (stdin is occupied by the export+cmd script). Inspector
  collectors almost never read stdin, so the impact is small, but this
  contract does **not** claim k8s exec stdin semantics match local / ssh.

### `read_file` requires `tar` in the container (known limitation)

K8s has no equivalent of docker's `get_archive`, so `read_file(path)` runs
`tar cf - <path>` inside the container (the `kubectl cp` mechanism) and parses
the streamed archive — enforcing the same single-regular-file / `not_a_file` /
10 MiB-cap semantics as the docker target. This means:

- A **distroless** / minimal container **without a `tar` binary** cannot be
  read: `read_file` raises `TargetError(kind="exec_failed")` with a hint that
  `tar` must be present. A `cat`-based fallback is a deliberate non-goal of
  this milestone (a follow-up) to keep the single-file tar logic unified with
  the docker target.
- A container without `/bin/sh` (distroless) likewise cannot run `exec` and
  raises `TargetError(kind="exec_failed")` — distinct from
  `k8s_unavailable` (the API + pod are healthy; only the command could not
  launch).
- Missing files surface the stdlib `FileNotFoundError` (not a `TargetError`),
  decided by tar's non-zero exit code + zero stdout bytes — never by parsing
  locale-specific stderr text.

### doctor

`hostlens doctor` does not special-case k8s targets: the generic `echo` probe
exercises the full path (kubeconfig + API + pod + container) and reports
`connectivity: ok` / `failed` / `skipped` (disabled) like any other target. A
k8s target with no `kubernetes-asyncio` installed reports `failed` with
`k8s_sdk_unavailable` rather than crashing the doctor run.

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

## `hostlens target import` — batch onboarding

Adding servers one at a time with `hostlens target add` does not scale to a
fleet of dozens. `hostlens target import <inventory>` batch-onboards targets
from an inventory file through a four-stage **read-only → write** pipeline:

```
InventorySource (parse, no network)  →  list[CandidateTarget]
        ↓
TargetProbe (reuse ExecutionTarget)  →  reachability + capabilities + OS fingerprint
        ↓
ImportPlan (human-readable diff)     →  to_add / skipped / failed_probe / invalid_candidate
        ↓  ── --dry-run stops here (no local write) ── │ ── --yes continues ──
save_targets_config (atomic 0600 + idempotent upsert)  →  ~/.config/hostlens/targets.yaml
```

The first three stages are read-only; the local write happens only on the
`--yes` path, last. This realises [`CLAUDE.md` §4.5](../../CLAUDE.md)'s
"plan / preview before any write" rule.

### Usage

```
hostlens target import <inventory> [--source ssh_config|yaml] \
    [--dry-run] [--yes] [--skip-unreachable|--include-unreachable] \
    [--concurrency N] [--json]
```

| flag | default | meaning |
|---|---|---|
| `<inventory>` | — | path to an ssh_config file or a yaml inventory (positional, required) |
| `--source ssh_config\|yaml` | content-sniff | force the inventory source; omit to detect by content / extension. Unknown value exits **2** (not 3) |
| `--dry-run` | **on** (default) | render the plan and exit 0 **without writing** `targets.yaml`. Mutually exclusive with `--yes` |
| `--yes` | off | write the resolved targets to `targets.yaml`. There is **no** per-row `y/N` prompt — `--yes` is the whole-batch confirmation |
| `--skip-unreachable` | **on** (default) | only onboard candidates that probed reachable |
| `--include-unreachable` | off | escape hatch: also register probe-failed candidates, written `enabled=False` (so later inspections don't probe them). Mutually exclusive with `--skip-unreachable` |
| `--concurrency N` | `10` | max simultaneous probe connections (semaphore bound), avoids a handshake storm across dozens of hosts |
| `--json` | off | emit the `ImportPlan` as a single machine-readable JSON document to stdout |

There is no `--yes` interactive prompt: absence of `--yes` **is** the dry-run
preview, which is why a missing `--yes` exits 0 rather than 1 (no
"non-interactive needs `--yes`" branch as `target remove` has).

### Honest note: `--dry-run` still probes remote hosts

`--dry-run` (the default) has **zero side-effect on the local `targets.yaml`**
— it never creates or modifies the file. But it **still opens read-only SSH
connections** to each inventory host to determine reachability and read
capabilities (the probe is the plan's input). "Offline" only describes the
**parse** stage (no network, no DNS): a malformed inventory reports a syntax
error without ever connecting. The probe stage does connect. So:

- A yaml inventory containing only `type: local` entries probes the **local
  machine** (subprocess `exec`) and needs no SSH at all.
- An ssh_config inventory probes the **real SSH hosts** even under `--dry-run`.
  Treat `--dry-run` against an ssh_config as "preview the write, but do reach
  out to the hosts read-only."

The `to_add` bucket lists every candidate's final connection address, so
`--dry-run` doubles as a **pre-write address audit point**: eyeball the plan
for unexpected hosts before passing `--yes`.

### Exit codes

| code | meaning |
|---|---|
| `0` | success — includes a dry-run preview, an empty inventory (empty plan), and `--include-unreachable` registering all-failed candidates |
| `1` | business failure — refused to run as root (`--yes` write path), or `--yes` written but **all** candidates failed probe and `--include-unreachable` was not passed (nothing to onboard) |
| `2` | parameter / config error — unknown `--source`, `--dry-run` + `--yes` together, inventory parse failure, a corrupt / illegally-placeholdered existing `targets.yaml`, or `load_settings()` failure |

`--dry-run` and `--yes` are **mutually exclusive** (fail-safe: prevents a
muscle-memory `--dry-run` plus an added `--yes` from silently writing) — passing
both exits 2. The write path (with `--yes`) **refuses to run as root**
(`EUID==0`) and exits 1 before touching `targets.yaml`; a `--dry-run` preview is
read-only locally and tolerates root.

### Behaviour summary (see the spec for the full contract)

- **name normalisation** — the source identifier (e.g. an ssh_config `Host`
  alias) is normalised to the `^[a-z][a-z0-9_\-]{0,63}$` target-name pattern
  (lowercase, illegal chars → `-`, leading non-letters stripped, truncated to
  64). A clash that cannot be normalised uniquely raises
  `ConfigError(ambiguous_target_name)`.
- **plaintext secrets are fail-closed** — an inventory carrying an inline
  `password` / `passphrase` value raises
  `ConfigError(plaintext_secret_forbidden)` at parse time; the plaintext is
  never read into a candidate, the plan, or any log. Credentials are carried
  only as **references** (`*_env` env var names matching `^[A-Z_][A-Z0-9_]*$`,
  or `key_path`), which the writer renders back as `${VAR}` placeholders.
- **idempotent** — a candidate whose normalised name already exists in
  `targets.yaml` lands in `skipped` (no overwrite); re-running `--yes` is safe.
  Updating an existing target (e.g. a changed IP) is **not** supported in this
  version — use `target remove` + re-import, or edit the yaml.
- **failure isolation** — a single host that times out / fails auth is bucketed
  into `failed_probe` (with a redacted `error_kind`, never a host IP /
  `user@host` / traceback) and never aborts the rest of the batch.

See the [`target-import` / `inventory-source` specs](../../openspec/specs/) for
the precise `CandidateTarget` / `ProbeResult` / `ImportPlan` schemas, the
`error_kind` closed set, and the fingerprint key allowlist.

### Demo Path

Two paths, offline-first (path 1 needs no SSH and no paid API).

**Path 1 — offline `local` inventory (no SSH):**

```bash
pip install -e ".[dev]"

# A yaml inventory with a single local entry — the probe runs against this
# machine, so no SSH is involved at any stage.
cat > /tmp/demo-inventory.yml <<'EOF'
hosts_local:
  demo-localhost:
    type: local
EOF

# 1. dry-run (the default): parse → promote → probe local (one read-only exec
#    to judge reachability + read capabilities for display) → render the
#    ImportPlan, marked DRY-RUN, nothing written.
hostlens target import /tmp/demo-inventory.yml --source yaml

# 2. write: --yes writes ~/.config/hostlens/targets.yaml (atomic 0600 + idempotent).
hostlens target import /tmp/demo-inventory.yml --source yaml --yes

# 3. idempotency: re-run --yes → demo-localhost lands in `skipped` (upsert,
#    not re-added).
hostlens target import /tmp/demo-inventory.yml --source yaml --yes

# 4. verify.
hostlens target list   # should list demo-localhost
```

**Path 2 — `ssh_config` inventory (probes real SSH hosts):**

```bash
# NOTE: --dry-run still opens read-only SSH connections to every Host in the
# config — "offline" only means the *parse* stage skips the network. Against
# real ssh hosts this step DOES connect (read-only) before showing the plan.
hostlens target import ~/.ssh/config --source ssh_config --dry-run

# A non-standard third-party inventory (e.g. ~/tizi/hosts, whose fields are
# not the Hostlens yaml schema) is read through the ssh_config source, not yaml:
hostlens target import ~/tizi/hosts --source ssh_config --dry-run
```

## `hostlens doctor --check-targets`

For each configured target, `doctor` reports:

- `connectivity`: `ok` / `failed` / `skipped` (disabled targets are
  always skipped)
- `credential_source`: `env_var` (placeholder expanded), `inline_plaintext`
  (literal in yaml — triggers a warning), `key_only` (SSH key path
  only, no password), `none` (local / docker / k8s targets — they carry no
  SSH credentials)
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
