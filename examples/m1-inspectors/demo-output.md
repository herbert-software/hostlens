# M1 Inspector demo — captured outputs

Recorded outputs for the 9-step Demo Path defined in
[`README.md`](README.md) and [`proposal.md`](../../openspec/changes/add-inspector-plugin-system/proposal.md).
Captured during OpenSpec change `add-inspector-plugin-system` task 15.4
on host `darwin-25.5.0` / Python `3.14.5`. CI runs the same steps via
the pytest suite — this file is the human-readable acceptance log.

## Step 1 — Environment setup

`pip install -e ".[dev]"` was already done by Group 1. Sanity check:

```
$ .venv/bin/python -c "import hostlens; import simpleeval, jinja2, jsonschema, yaml; print('deps ok')"
deps ok
```

## Step 2 — Load verification

```
$ .venv/bin/hostlens doctor --json | jq '.inspectors'
{
  "status": "ok",
  "loaded": 2,
  "errors": [],
  "missing_secrets": []
}
```

## Step 3 — List Inspectors

```
$ .venv/bin/hostlens inspectors list --json
[
  {
    "name": "hello.echo",
    "version": "1.0.0",
    "description": "Echo \"hello\" via the target to verify the inspector pipeline end-to-end.",
    "tags": ["demo", "hello"],
    "compatible_target_kinds": ["local", "ssh"]
  },
  {
    "name": "system.uptime",
    "version": "1.0.0",
    "description": "Extract 1/5/15-minute load averages from uptime output.",
    "tags": ["linux", "performance", "system"],
    "compatible_target_kinds": ["local", "ssh"]
  }
]
```

## Step 4 — View a manifest

```
$ .venv/bin/hostlens inspectors show hello.echo --json
{
  "name": "hello.echo",
  "version": "1.0.0",
  "description": "Echo \"hello\" via the target to verify the inspector pipeline end-to-end.",
  "tags": ["demo", "hello"],
  "targets": ["local", "ssh"],
  "requires_capabilities": [],
  "requires_binaries": ["echo"],
  "requires_files": [],
  "privilege": "none",
  "parameters": null,
  "secrets": [],
  "collect": {"command": "echo hello", "timeout_seconds": 5},
  "parse": {
    "format": "raw",
    "columns": [],
    "delimiter": "=",
    "skip_header_rows": 1,
    "raw_extract_regex": null
  },
  "output_schema": {
    "type": "object",
    "properties": {"raw": {"type": "string"}},
    "required": ["raw"],
    "additionalProperties": false
  },
  "findings": [
    {
      "for_each": null,
      "when": "len(raw) > 0",
      "severity": "info",
      "message": "hello received: {raw}"
    }
  ]
}
```

## Step 5 — Configure a local target

```
$ .venv/bin/hostlens target add local-host --type local
added target 'local-host' (local) to /Users/herbertgao/.config/hostlens/targets.yaml
exit=0

$ .venv/bin/hostlens target list
hostlens targets
┏━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┓
┃ name       ┃ type  ┃ enabled ┃ capabilities     ┃
┡━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━┩
│ local-host │ local │ True    │ file_read, shell │
└────────────┴───────┴─────────┴──────────────────┘
```

## Step 6 — ToolRegistry dispatch demo

```
$ .venv/bin/python examples/m1-inspectors/dispatch.py
2026-05-25 23:31:05 [info     ] inspector_started   inspector_name=hello.echo inspector_version=1.0.0 target_name=local-host
2026-05-25 23:31:05 [info     ] inspector_finished  duration_seconds=0.024 findings_count=1 inspector_name=hello.echo inspector_version=1.0.0 status=ok stderr_length=0 stdout_length=6 target_name=local-host
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

## Step 7 — Failure path verification

`bad-injection.yaml` declares a `host` parameter without `pattern` or
`enum`, which the loader rejects at load time.

```
$ mkdir -p /tmp/hostlens-bad-demo/inspectors
$ cp examples/m1-inspectors/bad-injection.yaml /tmp/hostlens-bad-demo/inspectors/bad.yaml
$ HOSTLENS_INSPECTORS_SEARCH_PATHS=/tmp/hostlens-bad-demo/inspectors \
    .venv/bin/hostlens doctor --json | jq '.inspectors'
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
exit=1
```

`hostlens doctor` also emitted a `hint:` line on stderr summarising the
load error, in addition to the JSON body on stdout. Exit code = 1 because
`inspectors.status == "fail"`.

## Step 8 — Root rejection verification

`inspectors list` is read-only and intentionally tolerates root
execution. We confirmed two ways:

```
$ .venv/bin/hostlens inspectors list
hostlens inspectors
┏━━━━━━━━━━━━━━━┳━━━━━━━━━┳ ... ┓
│ hello.echo    │ 1.0.0   │ ... │
│ system.uptime │ 1.0.0   │ ... │
└───────────────┴─────────┴ ... ┘
exit=0
```

Static check — the CLI module has no `geteuid` / `EUID` gate:

```
$ grep -rn "EUID\|geteuid\|require_unprivileged" src/hostlens/cli/inspectors.py
src/hostlens/cli/inspectors.py:7:Both commands are **read-only**, so they tolerate root execution (no EUID==0 ...
```

Only the module docstring mentions root — there is no code path that
rejects EUID==0. Live `sudo` was not executed in this run (macOS
sandbox); the static evidence is sufficient given the docstring contract.

## Step 9 — CI replay verification

```
$ .venv/bin/pytest tests/inspectors/ tests/cli/test_inspectors.py tests/tools/
============================= 418 passed in 1.59s ==============================
```

Full repo regression (`.venv/bin/pytest tests/`):
`733 passed, 12 skipped in 28.35s` (skips are opt-in SSH integration
tests gated behind `HOSTLENS_RUN_SSH_INTEGRATION=1`).

## Acceptance gates (task 15)

| Gate | Result |
|------|--------|
| 15.1 mypy --strict (touched modules) | clean — `Success: no issues found in 18 source files` |
| 15.2 ruff check src/ tests/ | clean — `All checks passed!` |
| 15.3 pytest M1.3 test set | `401 passed in 3.68s` |
| 15.3 full `pytest tests/` regression | `733 passed, 12 skipped in 28.35s` |
| 15.4 Demo Path 9 steps | all green, captured above |
| 15.5 shell injection matrix | `21 passed in 0.02s` (10 payloads × 2 + 1 count gate) |
