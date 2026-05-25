# M1 Inspector plugin system demo

5-minute demo of the Inspector plugin system landed by the
`add-inspector-plugin-system` OpenSpec proposal. Works on a clean macOS
or Linux dev host — **no SSH, no paid API, no remote access**.

The 9 steps below are the same as `proposal.md` Demo Path. Each step is
copy-paste-able and shows the expected output.

## Prerequisites

- Python 3.11+
- A clone of this repo

## Demo path (9 steps)

### 1. Environment setup (30s)

```bash
cd hostlens
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Sanity check:

```bash
python -c "import hostlens; import simpleeval, jinja2, jsonschema, yaml; print('deps ok')"
```

### 2. Load verification (30s)

```bash
hostlens doctor --json | jq '.inspectors'
```

Expected (the two builtin Inspectors load cleanly):

```json
{
  "status": "ok",
  "loaded": 2,
  "errors": [],
  "missing_secrets": []
}
```

### 3. List Inspectors (10s)

```bash
hostlens inspectors list --json
```

Expected: a JSON array of two `InspectorSummary` entries (sorted by
`name` ascending): `hello.echo` and `system.uptime`. Each entry carries
`name` / `version` / `description` / `tags` / `compatible_target_kinds`.

### 4. View a manifest (10s)

```bash
hostlens inspectors show hello.echo --json
```

Expected: the full manifest dump for `hello.echo`, including
`collect.command: "echo hello"`, `parse.format: raw`, and the single
finding rule `{when: "len(raw) > 0", severity: info, message:
"hello received: {raw}"}`. The `secrets` field is an empty array because
`hello.echo` does not use any secrets.

### 5. Configure a local target (30s)

This step uses the M1.1 `target` CLI from `add-execution-target-abstraction`:

```bash
hostlens target add local-host --type local
hostlens target list
```

Expected: `targets.yaml` now contains a single LocalTarget named
`local-host`, and `target list` shows it with `kind: local` and
`enabled: true`.

> If you've already run the M1.1 demo and your `~/.config/hostlens/targets.yaml`
> already has a `local-host`, skip this step. Step 6 below also works
> stand-alone — `dispatch.py` builds its own in-process `LocalTarget`
> instance.

### 6. ToolRegistry dispatch demo (60s)

This is the M2-style entry point: assemble a `ToolRegistry`, register
the default tools, and dispatch `run_inspector`:

```bash
python examples/m1-inspectors/dispatch.py
```

Expected output (structlog lines on stderr; the JSON dump is the
`RunInspectorOutput` on stdout):

```text
[info] inspector_started   inspector_name=hello.echo inspector_version=1.0.0 target_name=local-host
[info] inspector_finished  duration_seconds=0.03 findings_count=1 ... status=ok
{
  "target_name": "local-host",
  "inspector_name": "hello.echo",
  "findings": [
    {
      "severity": "info",
      "message": "hello received: hello\n",
      "evidence": {}
    }
  ]
}
```

The `dispatch.py` script builds its own in-process `TargetRegistry`
containing a single `LocalTarget("local-host")`, so it does not depend
on the persistent `targets.yaml` written in step 5 — that keeps the
demo reproducible on a clean checkout.

### 7. Failure path verification (60s)

Place a deliberately-malformed manifest under the user inspector
search path and confirm the loader rejects it with the expected error
kind:

```bash
mkdir -p /tmp/hostlens-bad-demo/inspectors
cp examples/m1-inspectors/bad-injection.yaml \
   /tmp/hostlens-bad-demo/inspectors/bad.yaml

HOSTLENS_INSPECTORS_SEARCH_PATHS=/tmp/hostlens-bad-demo/inspectors \
  hostlens doctor --json | jq '.inspectors'
echo "exit=$?"
```

Expected:

```json
{
  "status": "fail",
  "loaded": 2,
  "errors": [
    {
      "path": "/tmp/hostlens-bad-demo/inspectors/bad.yaml",
      "kind": "parameter_missing_charset_constraint",
      "detail": "parameter_missing_charset_constraint: parameter=host"
    }
  ],
  "missing_secrets": []
}
```

`hostlens doctor` exits with code `1` because `inspectors.status == "fail"`.
`bad-injection.yaml` declares a `host` parameter without a `pattern`
or `enum` constraint — the loader rejects this at **load time** (not
runtime) to make sure no shell-injection-prone string parameter can
reach `collect.command`. See the [5-layer defense](../../docs/operations/inspectors.md#shell-注入防御五件套)
in the inspectors ops guide.

Clean up:

```bash
rm -rf /tmp/hostlens-bad-demo
```

### 8. Root rejection verification (10s)

`inspectors list` and `inspectors show` are read-only, so they are
allowed to run as root (mirroring `target list`):

```bash
sudo hostlens inspectors list
echo "exit=$?"   # expect 0
```

Expected: `exit=0`, normal table output. There is no equivalent
write command for `inspectors` in M1 (no `inspectors add` / `remove`)
— Inspector manifests are authored by hand under
`~/.config/hostlens/inspectors/`, not via CLI mutation.

### 9. CI replay verification (30s)

```bash
pytest tests/inspectors/ tests/cli/test_inspectors.py tests/tools/ -v
```

Expected: all tests pass.

## Acceptance log

This demo was validated as part of OpenSpec change
`add-inspector-plugin-system`:

- `examples/m1-inspectors/dispatch.py` produced the `RunInspectorOutput`
  shown in step 6 against a real `LocalTarget("local-host")`.
- `examples/m1-inspectors/bad-injection.yaml` produced
  `parameter_missing_charset_constraint` per step 7.
- `pytest tests/inspectors/ tests/cli/test_inspectors.py tests/tools/`
  was green at the end of the implementation phase.
- `ruff check examples/m1-inspectors/dispatch.py` and
  `mypy --strict src/hostlens/inspectors/` were both clean.
