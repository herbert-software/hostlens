# Inspector Authoring Tutorial

5-minute guide to writing your first Inspector. Targets M1.

This tutorial picks a small, realistic scenario — **check the HTTP
status code of a web service** — and walks through manifest authoring,
loader debugging via `hostlens doctor`, validation via
`hostlens inspectors show`, and a one-shot run via the ToolRegistry
dispatch path.

Why dispatch instead of `hostlens inspect`? The M1 proposal scope
delivers `inspectors list/show` + the runner + Tool Registry handler.
The end-to-end `hostlens inspect <target> --inspector <name>` command
depends on the Report data model — that arrives in the next OpenSpec
proposal `add-report-data-model` (M1.6 + M1.7).

## Scenario

Check that an HTTP endpoint returns 200 OK. The Inspector takes the
endpoint URL as a parameter, runs `curl` on the target, and emits a
warning if the status code is anything other than `200`.

## Prerequisites

- Hostlens installed (`pip install -e ".[dev]"` from a repo clone)
- A configured local target (`hostlens target add local-host --type local`)

## Step 1: Write the manifest

Create the file `~/.config/hostlens/inspectors/web/check.yaml`:

```yaml
name: web.http.status
version: 1.0.0
description: Check that a web endpoint responds with HTTP 200.
tags: [web, http, health]
targets: [local, ssh]

requires_capabilities: [shell]
requires_binaries: [curl]
privilege: none

parameters:
  type: object
  required: [endpoint]
  properties:
    endpoint:
      type: string
      # Pattern is REQUIRED — any string parameter without `pattern` or
      # `enum` is rejected by the loader (5-defense layer #1). The
      # pattern here covers `https://host[:port][/path]` for ASCII-only
      # hosts and paths.
      pattern: "^https?://[A-Za-z0-9.\\-]+(:\\d+)?(/[A-Za-z0-9._/\\-]*)?$"
    expected_status:
      type: integer
      default: 200

collect:
  # `curl -s -o /dev/null -w '%{http_code}'` prints just the status code.
  # `endpoint` is a string parameter so it MUST go through the `sh`
  # filter (5-defense layer #2). `expected_status` is an integer, so
  # no `sh` filter is required.
  command: "curl -s -o /dev/null -w '%{http_code}' {{ endpoint | sh }}"
  timeout_seconds: 10

parse:
  format: raw
  raw_extract_regex: "^(?P<status>\\d{3})$"
  columns: [status]

output_schema:
  type: object
  properties:
    status:
      type: ["string", "null"]

findings:
  - when: "status and int(status) != expected_status"
    severity: warning
    message: "HTTP status {status} from {endpoint} (expected {expected_status})"
```

Key points (mapped to the 5-defense layers in
[inspectors.md](inspectors.md#shell-注入防御五件套)):

1. `parameters.endpoint` declares `pattern` — without it the loader
   raises `parameter_missing_charset_constraint`.
2. `collect.command` references `{{ endpoint | sh }}` — without `| sh`
   the loader raises `unquoted_parameter_in_command`.
3. No `secrets:` are used, so there is nothing to leak via Jinja2.
4. `requires_files: []` (omitted) — no path-injection risk surface.
5. `raw_extract_regex` is short (`^(?P<status>\d{3})$`), uses one
   named group matching one column entry, and contains no nested
   quantifiers / backrefs / atomic groups.

## Step 2: Check loader status with `hostlens doctor`

```bash
hostlens doctor --json | jq '.inspectors'
```

If the manifest is well-formed:

```json
{
  "status": "ok",
  "loaded": 3,
  "errors": [],
  "missing_secrets": []
}
```

`loaded` includes the 2 builtin Inspectors (`hello.echo` +
`system.uptime`) plus your new `web.http.status`.

### Common loader errors

If you see `"status": "fail"`, the `errors` array contains one entry
per failing file. The most common error kinds and how to fix them:

| `kind` | Cause | Fix |
|---|---|---|
| `manifest_too_large` | YAML file > 256 KB | Trim it. Inspector manifests should be small (<2 KB typical). |
| `unquoted_parameter_in_command` | `{{ host }}` instead of `{{ host \| sh }}` for a `type: string` param | Add `\| sh` filter. For arrays use `\| map('sh') \| join(' ')`. |
| `array_parameter_items_type_undetermined` | `parameters.x: { type: array }` without `items.type`, or `items` using `oneOf` / `anyOf` | Declare `items: { type: string, pattern: ... }` explicitly. |
| `secret_inlined_in_command` | `{{ PGPASSWORD }}` with `secrets: [PGPASSWORD]` | Reference via shell `$PGPASSWORD` (runner injects via env). |
| `parameter_missing_charset_constraint` | `type: string` parameter without `pattern` or `enum` | Add either constraint. The pattern should be ASCII-only. |
| `finding_when_invalid` | `when:` expression has syntax error or uses a forbidden simpleeval construct (lambda / listcomp / dunder) | Rewrite the expression with simple boolean / arithmetic ops. |
| `finding_message_invalid_aggregate_ref` | Aggregate-mode (no `for_each:`) message references `{var.attr}` | Either add `for_each:` or remove the attribute access. |
| `unsafe_raw_not_supported_in_m1` | Manifest declares top-level `unsafe_raw: true` | Not supported in M1 — refactor the manifest to use proper `sh` quoting. |
| `manifest_parse_error` | YAML syntax broken (or `!!python/object/apply` payload) | Fix the YAML. The error includes line + column. |
| `manifest_validation_error` | Pydantic field-level error (wrong type, missing required field, extra field) | Check `errors` detail for the specific field name. |
| `command_template_invalid` | Jinja2 template syntax error in `collect.command` | Fix the template (matching braces, valid filter chain). |
| `parse_json_not_object` | `parse.format: json` but the command's stdout isn't a JSON object at the top level | Wrap output in `{...}` or switch to a different format. |

If `errors` lists your file with one of the kinds above, fix the
manifest and re-run `hostlens doctor`. The loader runs at startup of
every CLI command, so the feedback loop is fast.

## Step 3: Verify the manifest with `hostlens inspectors show`

Once `doctor` says `ok`, confirm the manifest renders the way you
expect:

```bash
hostlens inspectors show web.http.status
```

You'll see the full manifest including `parameters` schema, `collect`
block, and the findings rule.

`--json` dumps the raw `InspectorManifest.model_dump()`:

```bash
hostlens inspectors show web.http.status --json
```

If the manifest declared `secrets:`, `show` would print just the names
— never the values. `parameters.<field>.default: "${ENV_VAR}"`
placeholders are also rendered as-is (not expanded).

## Step 4: Run the Inspector via ToolRegistry dispatch

Until `add-report-data-model` lands the `hostlens inspect` command, you
can exercise the runner end-to-end with a 30-line Python script. Save
this as `~/try-web-status.py`:

```python
import asyncio
from typing import cast

import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.registry import build_registry_from_search_paths
from hostlens.targets.base import ExecutionTarget
from hostlens.targets.config import LocalEntry
from hostlens.targets.local import LocalTarget
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry
from hostlens.tools.schemas.run_inspector import RunInspectorInput


async def main() -> None:
    settings = Settings()
    target_registry = TargetRegistry()
    entry = LocalEntry(name="local-host", type="local", enabled=True)
    target: ExecutionTarget = cast("ExecutionTarget", LocalTarget(name="local-host"))
    target_registry.register(target, entry)

    inspector_registry = build_registry_from_search_paths(
        settings.inspectors_search_paths, settings=settings
    ).registry

    tool_registry = ToolRegistry()
    register_default_tools(tool_registry)

    ctx = ToolContext(
        target_registry=target_registry,
        inspector_registry=inspector_registry,
        config=settings,
        logger=structlog.get_logger("authoring-tutorial"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )

    result = await tool_registry.dispatch(
        "run_inspector",
        RunInspectorInput(
            target_name="local-host",
            inspector_name="web.http.status",
            parameters={"endpoint": "https://example.com"},
        ),
        ctx,
    )
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
```

```bash
python ~/try-web-status.py
```

Expected (on a healthy `example.com`):

```json
{
  "target_name": "local-host",
  "inspector_name": "web.http.status",
  "findings": []
}
```

`findings == []` because `status == 200 == expected_status`, so the
`when` expression evaluates to False — no finding emitted.

Force a failure to verify the finding path:

```python
# parameters={"endpoint": "https://example.com/this-path-does-not-exist"}
```

Expected:

```json
{
  "target_name": "local-host",
  "inspector_name": "web.http.status",
  "findings": [
    {
      "severity": "warning",
      "message": "HTTP status 404 from https://example.com/this-path-does-not-exist (expected 200)",
      "evidence": {}
    }
  ]
}
```

## Step 5 (later): Migrate to `hostlens inspect`

Once `add-report-data-model` lands, the same invocation becomes:

```bash
hostlens inspect local-host \
  --inspector web.http.status \
  --param endpoint=https://example.com
```

The Inspector manifest itself does not change — the CLI just gains a
new entry point that wraps the same `InspectorRunner` you're already
calling from the script above.

## Iterating quickly

Inspector authoring loop in <30s per iteration:

```bash
# Watch loader status while you edit the manifest
while true; do
  clear
  hostlens doctor --json | jq '.inspectors'
  sleep 2
done
```

Or pair the script with `--watch`-style tooling (entr / watchexec) so
each save re-runs the dispatch:

```bash
echo ~/.config/hostlens/inspectors/web/check.yaml | entr -c python ~/try-web-status.py
```

## Reference

- Manifest field reference + 5-defense overview:
  [docs/operations/inspectors.md](inspectors.md)
- ExecutionTarget guide (target types, capability detection, SSH
  control connection pool): [docs/operations/targets.md](targets.md)
- Architecture deep dive (Inspector design rationale, Finding DSL
  semantics, ToolSpec wiring): [docs/ARCHITECTURE.md §4](../ARCHITECTURE.md#4-inspector-插件体系)
