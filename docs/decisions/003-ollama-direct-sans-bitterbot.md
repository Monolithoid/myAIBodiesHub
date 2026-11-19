# ADR 003 — Direct Ollama without Bitterbot for body-gateway

## Status

Adopted

## Context

`body-gateway` called Bitterbot (`/v1/chat/completions` on port 19001) to generate
the JSON actions sent to the robotic bodies. In practice, Bitterbot returned a 404
on that endpoint — its API was not compatible with the expected OpenAI format.

Bitterbot is a third-party dependency (upstream Docker Hub image) whose release
cycle and internal API surface we do not control. Moving it from the "brain" layer
to the role of a simple relay to Ollama added an unstable building block with no
immediate added value.

## Decision

`body-gateway` calls **Ollama directly** through its OpenAI-compatible API
(`/v1/chat/completions` on port 11434). The `bitterbot-gateway` service is
commented out in `docker-compose.yml` and can be re-enabled at any time.

```
Before: body-gateway → Bitterbot :19001 → Ollama :11434
After:  body-gateway → Ollama :11434
```

The model is configurable through `BRAIN_MODEL` (default `qwen3:1.7b`).
Authentication uses `BRAIN_API_KEY` (same value as `OLLAMA_API_KEYS`).

## What is lost

- **Long-term memory**: Bitterbot persists conversations in SQLite and builds a
  context over time. Without it, each inference starts again from an empty context.
- **Dream Engine**: autonomous memory consolidation every 2 hours. The dream ticks
  (`_dream_loop`) keep running but without persistent memory between cycles.

## What is kept

- The **full pipeline** of physical control (ROS 2 → NAO / drones).
- The **WebUI chat** with the wake word "ok nao".
- The **consistency of the JSON system prompt** — Ollama receives exactly the same
  structured prompt that Bitterbot received.
- The possibility of **bringing Bitterbot back** later once its API is stable:
  uncomment `bitterbot-gateway` in `docker-compose.yml` and set
  `BRAIN_URL=http://localhost:19001`.

## Consequences

- `bitterbot-net` stays declared in `docker-compose.yml` but no service actively
  uses it. It can be removed if Bitterbot is dropped for good.
- The `BITTERBOT_GATEWAY_TOKEN` variable is no longer needed in production but is
  kept in the comments to make re-enabling easier.

---

← [All decisions](../../README.md#architecture-decisions-adr) · [AI stack](../ia-stack.md) · [Chain test](../test-chain.md)
