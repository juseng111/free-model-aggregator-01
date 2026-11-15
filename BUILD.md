# Build notes — free-model-aggregator-01

Built 2026-11-15 (WIB) for make.fun bounty **free-model-aggregator-01**
("The best free AI model aggregator", $500, deadline 2026-11-16T02:30:00Z).
Entrant: GitHub `juseng111`, make.fun `juseng111`.
Payout wallet (Solana EOA, created 2026-11-15): `8J2swKr4Ncun9KHY33GXnfAKafmB38tXg4v14R8X7g66`
(0 SOL at creation; private key encrypted in `~/workspace/wallets/sol-eoa/`,
never printed anywhere, never committed to this repo).

## Files

| File | Purpose |
|---|---|
| `main.py` | The server. Stdlib-only Python 3. `python3 main.py --port 8080`; `python3 main.py --list` prints the registered catalog. |
| `catalog.json` | Vendored fallback catalog: 10 providers, 20 models. |
| `tests/test_server.py` | 11 unittest tests against a mock provider env on localhost. |
| `README.md` | Includes the three live make.fun badges (repo name filled in). |
| `LICENSE` | MIT (OSI-approved). |
| `.makefun/owner.json` | `{"wallet": "8J2s...X7g66"}` — repo-owner claim. |

## Design (from the bounty description + requirements in /tmp/req01.json)

- Catalog discovery from `MAKEFUN_ENV_URL` (`/v1/models`, `/models`,
  `/catalog`, `/providers`, `/api/*`); a full env catalog (10+ models)
  replaces the fallback, a partial listing only enriches it.
- Deterministic ranking: quality + capability match + context fit −
  failure penalty; ties break by model id. No wall clock, no unseeded
  randomness.
- Failover: ranked order, max 5 attempts; 402 / 429 / 5xx / timeout /
  malformed answer moves to the next model.
- Wires: OpenAI `/v1/chat/completions` (JSON + SSE, tools, JSON mode),
  Anthropic `/v1/messages` (JSON + SSE, tools, system), Gemini
  `/v1beta/models/{model}:generateContent` / `:streamGenerateContent`,
  plus `GET /v1/models`, `/catalog`, `/health`.
- Size caps respected: one file max 8 MiB, tree max 16 MiB (actual tree
  is ~45 KB — see `du` below). No provider keys anywhere in the repo.

## Test log (2026-11-15, `python3 -m unittest discover -s tests -v`)

```
test_anthropic_messages ... ok
test_anthropic_streaming ... ok
test_gemini_generate ... ok
test_list_is_deterministic ... ok
test_long_context_excludes_small_models ... ok
test_models_endpoint ... ok
test_openai_chat_deterministic_repeat ... ok
test_openai_chat_fails_over_from_402 ... ok
test_openai_chat_streaming ... ok
test_openai_chat_tools_and_json_mode ... ok
test_openai_chat_vision_prefers_vision_model ... ok
Ran 11 tests in 0.872s — OK
```

The mock env makes the top model (`fjord-free-opus`, quality 95) always
answer 402, so the failover test proves the server falls back to
`atlas-free-large` and still serves the request. `--list` is byte-identical
across runs (10 providers, 20 models).

## Local run check

```
$ python3 main.py --list | head -5
{
  "providers": [
    {
      "id": "atlas",
```

## What was NOT done (blockers — see report to parent)

1. **GitHub repo not created.** `create_repository` via the GitHub
   connector returned `403 Resource not accessible by integration`.
   The repo is ready locally (git init + commit below); it needs to be
   created on github.com first (public, name `free-model-aggregator-01`,
   no README/license — this repo already has both), then pushed:
   ```
   git remote add origin https://github.com/juseng111/free-model-aggregator-01.git
   git push -u origin main
   ```
   Then set the repo About: website
   `https://make.fun/bounty/free-model-aggregator-01`, topics
   `makefun,ai-agents,bounties` (required: website + `makefun` topic).
2. **Not submitted to make.fun.** Submission needs (a) a Solana wallet
   linked to the make.fun account (profile still "No wallet linked"),
   (b) a signed entry message (within 10 minutes of submitting), and
   (c) a transcripts bundle. No kit was downloaded and no make.fun CLI
   was run in this build, so no `grade-local` / `check` was run either.
3. **Transcripts / paid places.** This harness keeps no session log
   that the kit's `bundle` can read. The requirements say a handed-over
   session is ranked but takes **no paid place** (`handed_over_places:
   "none"`), and every transcript must account for ≥30% of the code's
   lines with ≥5 assistant turns and ≥2 revisions of an entrypoint.
   A build log was kept in this file instead; do NOT fabricate a
   transcript bundle. To compete for a paid place, the build must be
   re-run in a client whose sessions are recorded (Claude Code / Codex
   via the kit's recorder), before the deadline.
