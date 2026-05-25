# M1 Targets demo

5-minute demo of the `ExecutionTarget` abstraction landed by the
`add-execution-target-abstraction` OpenSpec proposal. Works on a clean
POSIX dev machine (macOS / Linux) — no SSH server, no paid API.

## Prerequisites

- Python 3.11+
- Docker (for the SSH integration step)
- `git` clone of this repo

## Demo path (10 steps)

### 1. Install

```bash
cd hostlens
pip install -e ".[dev]"
```

Sanity check:

```bash
python -c "import asyncssh, aiofiles, psutil; print('deps ok')"
```

### 2. Start the demo sshd container

```bash
docker run -d --rm \
  -p 2222:2222 \
  -e USER_NAME=hostlens \
  -e PASSWORD_ACCESS=true \
  -e USER_PASSWORD=demo \
  --name hostlens-demo-sshd \
  linuxserver/openssh-server
```

Wait ~10s for the container to warm up, then inject the `AcceptEnv`
allowlist so subsequent env-passthrough steps work:

```bash
docker exec hostlens-demo-sshd sh -c \
  "echo 'AcceptEnv HOSTLENS_TEST_*' >> /config/sshd/sshd_config && pkill -HUP sshd.pam"
```

### 3. Add a LocalTarget

```bash
hostlens target add my-local --type local
```

### 4. Add the SSHTarget

```bash
export HOSTLENS_DEMO_SSH_PASSWORD=demo
hostlens target add my-ssh \
  --type ssh \
  --host localhost \
  --port 2222 \
  --user hostlens \
  --password-env HOSTLENS_DEMO_SSH_PASSWORD
```

### 5. List targets

```bash
hostlens target list --json | jq
```

Expect both `my-local` and `my-ssh` with their `kind` / `enabled`
fields and an initial `capabilities` set (capability probing happens
lazily on first `exec`, so this list may be the constructor-time
baseline `[shell, file_read]`).

### 6. Probe connectivity

```bash
hostlens target test my-local
hostlens target test my-ssh
```

Both should return `connectivity: ok` and report any extra
capabilities discovered through `which` (e.g. `systemd` / `docker_cli`
if the binary is on the remote PATH).

### 7. doctor

```bash
hostlens doctor --json | jq .targets
```

The `targets` section shows per-target `connectivity` /
`credential_source` / `capabilities`. If `credential_source ==
"inline_plaintext"` for any target, doctor will print a warning but
still exit 0.

### 8. Root rejection (write commands)

```bash
sudo hostlens target add bad-from-root --type local
echo "exit=$?"            # expect 1
```

Expected output on stderr:

```
hostlens target add: refused to run as root (EUID=0).
Re-run as the non-privileged user that owns ~/.config/hostlens/.
```

`targets.yaml` is **not** modified. `sudo hostlens target list` and
`sudo hostlens target test my-local` still work — they are read-only.

### 9. SSH connection-pool verification (in-process)

`hostlens target test` spawns a new Python process per call, so it
can't observe pool reuse. Verify in a single REPL instead:

```python
import asyncio
from pathlib import Path
from unittest.mock import patch
import asyncssh
from hostlens.core.config import Settings
from hostlens.targets.config import load_targets_config, build_registry_from_config

settings = Settings()
cfg = load_targets_config(settings.targets_config_path)
reg = build_registry_from_config(cfg, settings)
target = reg.get("my-ssh")

with patch.object(asyncssh, "connect", wraps=asyncssh.connect) as m:
    async def go():
        for _ in range(3):
            await target.exec("echo hi", timeout=5)
    asyncio.run(go())
    print("asyncssh.connect call count:", m.call_count)  # expected: 1
```

Three sequential `exec` calls should trigger exactly **one**
`asyncssh.connect` (the control connection is reused; each `exec`
opens a new channel on it).

### 10. (Optional) list_targets ToolSpec dispatch

Requires the M2 Inspector registry stub to be replaced — coming in
the next OpenSpec proposal `add-inspector-plugin-system`. Until then
this step is **skipped**.

```python
# Skipped: depends on add-inspector-plugin-system landing the real
# InspectorRegistry. With M1 alone, the Tool Registry dispatch path
# can be exercised through unit tests
# (tests/tools/test_list_targets_real_registry.py).
```

### Cleanup

```bash
docker rm -f hostlens-demo-sshd
hostlens target remove my-ssh --yes
hostlens target remove my-local --yes
```

## Acceptance log

The Hostlens implementation of this demo was validated in CI / locally
as part of OpenSpec change `add-execution-target-abstraction`:

- `pytest` (unit + non-integration): 368 passed
- `pytest tests/targets/test_ssh_integration.py`: 12 passed (against
  the real `linuxserver/openssh-server` container)
- `mypy --strict`: 11 source files clean
- `ruff check`: clean
- Step 8 (root rejection): verified via mocked `os.geteuid` in
  `tests/cli/test_target.py`
- Step 9 (connection pool reuse): asserted by
  `tests/targets/test_ssh.py::test_first_exec_opens_connection_subsequent_exec_reuses`
  and the integration test `test_control_connection_reuse_via_ss`
