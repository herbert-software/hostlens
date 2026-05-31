# Incident-pack snapshot tests (M2.8)

These tests prove the **offline replay pipeline deterministically reproduces 8
incident scenarios' findings** from canned host state and scripted model turns
— zero Anthropic API quota, zero SSH, zero real host. Be honest about scope:

- **Real**: the Inspector + Finding DSL turning canned failure-state stdout
  into expected-severity findings, and the whole pipeline (Planner → Loop →
  ToolsAdapter → runner → ReplayTarget) round-tripping under strict consumption.
- **Scripted**: the model's turns are hand-authored in `_scenarios.py` and
  replayed verbatim from the cassette — the model does not *decide* which
  inspectors to call. The tool_use-sequence assertion guards against pipeline
  regressions (a dropped / renamed invocation), not against model reasoning;
  exercising real model diagnosis is a `live`-mode / future concern.

The full `--intent` Planner pipeline is driven over a **double replay layer**:

```
PlannerAgent → AgentLoop → ToolsAdapter → run_inspector → InspectorRunner
        │                                                        │
   LLM layer                                              execution layer
        │                                                        │
  PlaybackBackend ← cassette                          ReplayTarget ← fixture
  (src/hostlens/demo/scenarios/<key>/cassette.jsonl)  (src/hostlens/demo/scenarios/<key>/fixture.json)
```

Everything runs under a **frozen clock** (`_harness.FROZEN_DT`) so the one
`sampling_window` inspector (`log.tail.error_burst`) renders byte-stable
commands the `ReplayTarget` can match.

## Layout

| Path | Role |
|---|---|
| `_scenarios.py` | the 8 scenarios: inspectors + canned failure stdout + scripted narrative (single source) |
| `_harness.py` | `build_incident_planner`, `assert_incident_snapshot`, deterministic `project_planner_result` |
| `_generate.py` | env-gated generator (the re-record procedure); not collected by `pytest` (underscore name) |
| `test_<scenario>.py` ×8 | one snapshot test per scenario (replay only) |
| `test_drift.py` | strict-consumption + unit-level `ReplayMiss` drift guards |
| `snapshots/<scenario>.md` ×8 | committed deterministic projection baselines |
| `../../src/hostlens/demo/scenarios/<scenario>/fixture.json` ×8 | ReplayTarget command fixtures (migrated to product package; SOT) |
| `../../src/hostlens/demo/scenarios/<scenario>/cassette.jsonl` ×8 | PlaybackBackend cassettes (migrated to product package; SOT) |

## What the snapshot compares

`project_planner_result` renders a deterministic projection and the test
asserts it byte-equals the committed `snapshots/<scenario>.md`:

- **narrative** — `PlannerResult.narrative` (from the cassette),
- **findings** — `(severity, message, tags)` sorted by `(severity_rank,
  message)` so order does not depend on inspector/pipeline collection order,
- **tokens** — cumulative `input` / `output` totals (from the cassette usage).

It explicitly **excludes** duration / Rich decoration / run_id / timestamps —
the Rich `render_planner_result` terminal output is never compared (it carries
a wall-clock `duration_s` and width-dependent wrapping).

The test also asserts `ReplayTarget.misses == []` (strict-consumption: the
primary drift signal, since `ToolsAdapter.dispatch` absorbs the `ReplayMiss`
exception) and that the scenario's core inspectors appear in the tool_use
sequence.

## Re-recording (when a command or the tools schema changes)

Both artifacts are produced by the generator — **no API key required**. It
records cassettes by driving the real pipeline with a `RecordingBackend`
wrapping a scripted `FakeBackend`, and builds fixtures by capturing the exact
rendered commands from a real `InspectorRunner` run.

```sh
# regenerate all 8 scenarios' fixtures + cassettes + snapshots
HOSTLENS_GENERATE_INCIDENTS=1 pytest tests/incidents/_generate.py -q

# one scenario only
HOSTLENS_GENERATE_INCIDENTS=1 HOSTLENS_GENERATE_ONLY=cpu_saturation \
  pytest tests/incidents/_generate.py -q
```

After regenerating, the cassette commit gate must pass before committing:

```sh
python scripts/cassette_lint.py     # secret-scan + schema + duplicate keys; exit 0 required
```

### Editing a scenario

Edit `_scenarios.py` only (intent / narrative / inspectors / canned stdout),
then regenerate. The generator asserts each inspector reaches `status=ok` with
≥1 finding, so an authoring mistake fails loudly at generation time.

The committed fixtures / cassettes / snapshots are **generator-owned**. The
snapshot + `misses == []` guards catch any manual edit that changes a finding
or a rendered command, but a hand-edit to a fixture field that does **not**
drive a finding (e.g. flipping an already-reachable endpoint, or editing
stderr / whitespace) can leave the suite green while making the fixture
misrepresent host state. Always regenerate rather than hand-editing fixtures.

### Synthetic-data discipline

The cassette gate (`hostlens.core.redact`) rejects real-looking data committed
to git. Canned data must avoid IPv4 literals, `/home` · `/Users` paths, emails,
and dotted FQDNs with a flagged suffix. Network endpoints use **single-label**
service names (`database:5432`, `payments:443`) so they satisfy the manifest
`host:port` pattern without tripping the `hostname_or_fqdn` rule. Chinese
strings in `.py` use ASCII punctuation (ruff RUF001).

See [tests/fixtures/cassettes/README.md](../fixtures/cassettes/README.md) for
the cassette file format and the general (API-key) record mode, and
[docs/operations/inspectors.md](../../docs/operations/inspectors.md) for
`sampling_window` and `ReplayTarget` usage.
