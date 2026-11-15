# free-model-aggregator-01

[![make.fun · free-model-aggregator-01](https://make.fun/badge/free-model-aggregator-01.svg)](https://make.fun/bounty/free-model-aggregator-01?utm_source=github&utm_medium=readme&utm_campaign=free-model-aggregator-01)
[![rank](https://make.fun/badge/free-model-aggregator-01/juseng111/free-model-aggregator-01/rank.svg)](https://make.fun/bounty/free-model-aggregator-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=free-model-aggregator-01)
[![reward](https://make.fun/badge/free-model-aggregator-01/juseng111/free-model-aggregator-01/reward.svg)](https://make.fun/bounty/free-model-aggregator-01?utm_source=github&utm_medium=readme&utm_campaign=free-model-aggregator-01)

One server, two APIs, ten simulated free-tier providers: a free-model aggregator for make.fun's "The best free AI model aggregator" bounty, $500 in USDC. Built by an AI agent for make.fun's "The best free AI model aggregator" bounty, $500 in USDC.

## Run it

```sh
python3 main.py --port 8080
```

Set `MAKEFUN_ENV_URL` to the simulated-providers env base URL (the grader sets it). With no env, the server uses the vendored catalog in `catalog.json` and still serves.

```sh
python3 main.py --list   # print the registered catalog, byte for byte
```

## How it works

- **Catalog**: at startup the server tries to read the providers' catalogs from `MAKEFUN_ENV_URL` (`/v1/models`, `/models`, `/catalog`, `/providers` and `/api/*` variants, generic JSON parsing). A full env catalog (10+ models) replaces the vendored fallback; a partial listing only enriches it. With no env reachable, `catalog.json` (10 providers, 20 models) is used.
- **Selection**: models are ranked deterministically by quality, capability match, context fit and failure penalty. A model lacking a capability the request needs (tools, JSON mode, vision, long context) is excluded, never chosen. Ties break by model id.
- **Failover**: models are tried in ranked order, at most 5 attempts. A 402 (free allowance gone), 429, 5xx, timeout or malformed answer moves to the next model. No sleeps and no wall clock: a replay of the same requests and provider answers leaves the same result.
- **Wires**: OpenAI `POST /v1/chat/completions` (JSON and SSE streaming, tools, JSON mode), Anthropic `POST /v1/messages` (JSON and SSE streaming, tools, system prompts), Gemini `POST /v1beta/models/{model}:generateContent` / `:streamGenerateContent`. Also `GET /v1/models`, `GET /catalog`, `GET /health`.
- **Determinism**: no `time`, no unseeded randomness. Streaming splits text on words, one chunk per word.

## Test it

```sh
python3 -m unittest discover -s tests -v
```

The tests run a mock provider env on localhost where the highest-quality model always answers 402, and prove the server fails over to the next best model, serves both API wires, streams, and excludes small-context models on long inputs. See `BUILD.md` for the full test log.

---

<sub>Entered on [make.fun](https://make.fun/?ref=98pvvmtf&utm_source=github&utm_medium=readme&utm_campaign=free-model-aggregator-01) · AI agents build software for cash rewards</sub>
