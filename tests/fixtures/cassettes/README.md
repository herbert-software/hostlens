# Cassettes

JSON Lines pre-recorded LLM responses consumed by
`hostlens.agent.backends.playback.PlaybackBackend` so integration tests
exercise the Agent loop without burning Anthropic API tokens. One file
per scenario; one record per line. Cassette-miss fails fast (no
fallback to real API).

## File format

Each line is a self-contained JSON object:

```json
{"request": {...}, "response": {...}, "tools_schema_hash": "<sha256-hex>"}
```

- `request`: the matching key payload. `PlaybackBackend` hashes
  `{"model", "messages", "tools_count"}` (SHA-256, `sort_keys=True`).
  `system`, `max_tokens`, full `tools`, and `timeout` are NOT in the
  key (see spec for trade-off).
- `response`: a valid `MessageResponse` payload (`id`, `model`, `role`,
  `content[]`, `stop_reason`, `usage`); validated at load time.
- `tools_schema_hash` (optional, lint-only): SHA-256 of tools schema at
  record time; `--check-schema-drift` warns on drift, never blocks.

## Test modes (`HOSTLENS_LLM_MODE`)

The `llm_cassette` fixture (`tests/conftest.py`) dispatches on
`HOSTLENS_LLM_MODE`. Resolution lives **only** in the test fixture layer —
production `create_backend` neither reads nor knows about this env var.

| Mode | When | API key | Cassette |
|---|---|---|---|
| `replay` (default, unset/empty) | CI, local default | not needed | reads; missing file fails fast |
| `record` | local, opt-in | `ANTHROPIC_API_KEY` required | writes (overwrites whole file) |
| `live` | local debugging | `ANTHROPIC_API_KEY` required | neither read nor written |

Any other value fails fast naming the legal set; record/live without
`ANTHROPIC_API_KEY` calls `pytest.fail` rather than returning a backend
that would only 401 on first call.

## incident-pack cassettes — migrated out of this directory

The 8 incident-pack cassettes no longer live here. They were migrated to the
product package as `src/hostlens/demo/scenarios/<scenario>/cassette.jsonl`
(SOT for both the `hostlens demo` command and the `tests/incidents` snapshot
tests; see add-demo-cli). They are **not** produced by
`HOSTLENS_LLM_MODE=record`: they are generated offline (no API key) by the
incident generator, which drives the real Planner pipeline with a
`RecordingBackend` wrapping a scripted `FakeBackend` over a `ReplayTarget`. To
refresh them, see [tests/incidents/README.md](../../incidents/README.md). The
file format and lint below apply to them identically — note `cassette_lint.py`
scans the migrated location too.

## Recording flow (`HOSTLENS_LLM_MODE=record`)

There is a real recorder (`tests/support/cassette_recording.py`
`RecordingBackend`). To produce or refresh a cassette, run the test that
uses `llm_cassette("<name>")` in record mode:

```sh
HOSTLENS_LLM_MODE=record ANTHROPIC_API_KEY=... \
  pytest tests/agent/test_planner_replay.py::test_planner_health_check
```

The fixture wraps a live `AnthropicAPIBackend` in `RecordingBackend`,
collects the whole scenario's `(request, response)` pairs in memory, runs
the sensitive-content gate, and atomically overwrites the cassette
(`name.jsonl`) at test teardown — never appends.

After recording, run the lint and require exit 0 before committing:

```sh
python scripts/cassette_lint.py
```

`cassette_lint.py` does secret-scan + duplicate request-key detection
(see below).

### Explicit naming (never nodeid-derived)

`llm_cassette("planner_health_check")` maps to
`tests/fixtures/cassettes/planner_health_check.jsonl`. The name is the
literal argument — it is **not** derived from the test nodeid.

- Do not reuse a name across unrelated scenarios — the request-key hash
  domain is per-file, so collisions are silent.
- For `parametrize`d tests, fold the parameter into the explicit name
  (one parameter → one cassette, e.g.
  `llm_cassette(f"planner_{case_id}")`). Sharing one cassette across
  parametrized cases mixes their request-key domains.

### Governance constraints (detect-and-reject, not scrub)

Recording is deliberately strict so a committed cassette can never carry
a real secret or real host:

- **Synthetic byte-stable inputs only.** Record mode only runs against a
  byte-stable synthetic target: `type == local` whose `TargetEntry.tags`
  contains `cassette-synthetic`. Any `ssh` / `docker` / `k8s` target — or
  a bare `local` target without that tag — is treated as **real** and
  rejected by `guard_record_targets` at the assembly layer (the fixture
  enforces this before returning the recorder; a test author cannot
  bypass it). Synthetic `tool_result`s must freeze clocks / UUIDs /
  usernames / paths so the bytes are stable across re-records.
- **Request side is not scrubbed and not re-keyed.** The recorder writes
  the same canonical request subset (`{model, messages, tools_count}`)
  that `PlaybackBackend` keys on; recording does not rewrite or re-key it.
- **Detect-and-reject, never silent scrub.** Before persisting, both the
  serialized canonical request **and** the serialized response pass
  through `detect_sensitive_text`. A hit raises
  `SensitiveCassetteContentError` (names the firing rule + side, never
  echoes the matched value), poisons the recorder, and the offending
  record is **not** written. The fix is to make the fixture byte-stable,
  not to scrub after the fact.

### `HOSTLENS_ALLOW_REAL_TARGET_RECORD=1` (risky escape hatch)

Off by default. Setting it to `1` disables the real-target guard so
record mode will run against real `ssh` / `docker` / `k8s` / bare-`local`
targets. **A real hostname / IP / path can then land in the committed
cassette.** Use only for one-off local debugging, never in CI, and audit
the resulting cassette before committing.

### Multi-instance / xdist

Each record record carries a `tools_schema_hash` written automatically by
the recorder (default `ensure_ascii`, to match the CI
`--current-tools-hash` computation). A second `RecordingBackend` pointed
at the same cassette path in the same run fails fast (the in-process
active-path registry would otherwise be clobbered on teardown).
`pytest-xdist` is rejected outright in record mode — the in-process
registry cannot be shared across worker processes.

## What `cassette_lint.py` checks

- Each record validates against `MessageResponse`.
- No line contains a pattern matched by `hostlens.core.redact`.
- `--check-schema-drift --current-tools-hash <hex>` warns on drift.
