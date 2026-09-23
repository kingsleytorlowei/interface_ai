# Computer-Use Automation System

LLM discovers a flow once → recorded as a typed, versioned capability artifact → replayed
deterministically (no LLM) with typed inputs/outputs, explicit error handling, guardrails, and
human-in-the-loop handoff on the live session.

Design write-up: [REPORT.md](REPORT.md) · Example runs: [evidence/](evidence/)

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                  # install deps into .venv
uv run playwright install chromium       # browser for the web surface
uv run pytest                            # tests
uv run lint-imports                      # architectural boundary contracts
```

## Target app: CoreOne Teller (mock core banking)

`src/mock_bank` is a deliberately legacy mock of a vendor core-banking product: framesets,
table layouts, unlabelled inputs, cryptic field names, no test IDs. Two tenant variants run the
same product with different branding, labels, version and table layout.

```bash
uv run python -m mock_bank --variant pinnacle  --port 8001
uv run python -m mock_bank --variant riverbend --port 8002
```

Sign on with `teller1` / `demo-only-password` (fake app, fake credential).

| Member | Behaviour |
|---|---|
| `12345` | Happy path (Share Savings $4,210.37) |
| `23456` | MEMBER ALERT interstitial (needs a human) |
| `34567` | Access denied (restricted account) |
| `99999` | Not found |
| `12a`   | App-side validation error |

Injected faults (test harness only; outside the agent's allowlist):

```bash
curl -X PUT localhost:8001/__admin/faults -H 'content-type: application/json' \
  -d '{"latency_ms": 0, "transient_failures": 1, "fatal_errors": 0, "broadcast_notices": 1, "session_ttl_s": 900}'
curl -X POST localhost:8001/__admin/expire-sessions
curl -X POST localhost:8001/__admin/reset
```

## Configuration

Copy `.env.example` to `.env` and fill in your model API key. Secrets are never committed.

## Demo path

_TODO: exact commands — (1) run the mock target app, (2) discovery on a goal, (3) replay the
artifact with params, (4) replay hitting an error/business outcome, (5) escalation & handoff._

## Running without live services

_TODO_
