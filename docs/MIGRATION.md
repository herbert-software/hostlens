# Migration: M1 to M2

M2 introduces the `LLMBackend` abstraction (see
`openspec/changes/add-llm-backend-protocol/`). M0 / M1 configs continue
to load (the new `backend` and `agent` sections are optional in the
schema), but any command that drives the Agent loop now requires the
two new sections to be present.

## Minimum config diff

Add the following blocks to `~/.config/hostlens/config.yaml`:

```yaml
# Section 1 — backend ("with whom / how to authenticate")
backend:
  type: anthropic_api
  api_key: ${ANTHROPIC_API_KEY}
  # base_url: null            # optional; used for self-hosted proxies
  # cassette_path: null       # required when type=playback

# Section 2 — agent ("which model / loop knobs")
agent:
  primary_model: claude-opus-4-7
  # fallback_model: null      # reserved for later milestones
  health_check_model: claude-haiku-4-5
  max_turns: 20
  token_budget_input: 100000
  token_budget_output: 30000
  health_check_timeout_seconds: 10
```

Both sections are independent. `backend` covers authentication and
endpoint selection; `agent` covers model identifiers and loop knobs.

## What stays the same

- All M0 / M1 CLI commands continue to load without the new sections.
- Existing target / inspector YAML manifests are unchanged.
- `hostlens doctor` automatically picks up the new `backend` section
  when present and reports `api_key_set` plus an `api_key_fingerprint`
  (first 4 + last 4 chars). The full key never appears in output.

## What changes if you do not add the sections

- Any future command that calls `create_backend(settings)` raises
  `ConfigError("backend.type required to use LLM features")`. M2
  Agent-loop commands (`hostlens inspect --intent ...`) will be the
  first consumers; the M1 inspect path still works without `backend`.

## Backend type placeholders

`backend.type` accepts `bedrock`, `vertex`, and `claude_subscription`
in the schema, but `create_backend` raises `NotImplementedError` for
all three. They land in M10.5 / 1.0; do not use them yet.

## Rollback

Remove the `backend` and `agent` blocks from the config. Pydantic v2's
`extra="ignore"` makes the rollback non-destructive: any commits
referencing the new sections still load against an M1 binary.
