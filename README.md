# Computer-Use Automation System

An LLM discovers a workflow in a legacy UI **once**; the result is recorded as a typed,
versioned **capability artifact**; after a human approves it, the artifact is **replayed
deterministically, without an LLM**, with typed inputs and outputs, explicit error handling,
guardrails on every action, and a handoff to a human on the live session when a screen needs
a person.

Design write-up: [REPORT.md](REPORT.md) · Runs and screenshots: [evidence/](evidence/)

## Modules

Two drivers (discovery, replay) sit on one guarded chokepoint (`GuardedSession`) and never
depend on each other; the only thing they share is the artifact schema. The boundaries are
enforced by import-linter contracts (see [Tests & contracts](#tests--contracts)).

| Module | Job |
|---|---|
| `cua.schema` | Pure data contracts: capability artifact, targets, steps, state signatures, observations, run results. No I/O. |
| `cua.surface` | The perception/action port (`observe`, `resolve`, `act`) and its Playwright + Chromium web adapter. |
| `cua.policy` | Guardrails: origin allowlist, action risk classes, Allow / Deny / RequireApproval, redaction. |
| `cua.control` | Control of a live session: the lease (who holds it), approval and intervention requests, resume/abort. |
| `cua.session` | `GuardedSession`: the single chokepoint every action flows through (policy → lease → surface → evidence). |
| `cua.evidence` | Run directories, redacted JSONL event log, failure snapshots. |
| `cua.secrets` | Resolves `env:NAME` credential references at the last moment; values never reach artifacts or logs. |
| `cua.apps` | Per-app knowledge shared across capabilities: the state library (error pages, interstitials, notices). |
| `cua.store` | Artifacts under `catalog/`: versions, and the draft → verified → approved gate. |
| `cua.discovery` | The LLM observe → decide → act loop and the recorder that turns actions into parameterized steps. The only place an LLM is used. |
| `cua.replay` | Deterministic step execution, state classification, recovery, and the typed result. Never uses an LLM. |
| `cua.operator` | Operator console (web) over `cua.control`: approvals, handoffs, pause, take over. |
| `cua.cli` | Composition root: `cua discover`, `cua approve`, `cua replay`. |
| `mock_bank` | The target: a deliberately legacy mock core-banking app with two tenant variants. |

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                # install dependencies into .venv
uv run playwright install chromium     # browser for the web surface
cp .env.example .env                   # then set ANTHROPIC_API_KEY (discovery only)
```

`.env` also holds the mock app's teller credentials (`COREBANK_USER`, `COREBANK_PASSWORD`),
which the engine resolves at run time. `.env` is git-ignored.

## Demo path

Run each command from the repo root. Replay prints a JSON `RunResult`; the listings below
are trimmed to the fields that matter. Every run also writes a redacted evidence directory
under `evidence/<run_id>/`.

**1. Start the target app** (keep it running in its own terminal).

```bash
uv run python -m mock_bank --variant pinnacle --port 8001
```

**2. Discover the flow with the LLM** (needs `ANTHROPIC_API_KEY`; about 30 s and ~$0.10 on
`claude-opus-5`). Add `--headed` to watch. Discovery records a draft, then replays it once
without the LLM to verify it.

```bash
uv run cua discover goals/corebank.member.lookup_balance.yaml
```
```
discovery: recorded (flow recorded) in 5 turns; usage {'input_tokens': 10, 'output_tokens': 1284, 'cache_read_input_tokens': 21472, 'cache_creation_input_tokens': 8576}; evidence evidence/<run_id>
  review: step click_search_button is recorded as reversible; lower it to read_only at review if it only queries
verification: success; evidence evidence/<run_id>
draft saved: catalog/corebank/capabilities/corebank.member.lookup_balance@0.1.1.json
```

The repo already holds an approved `0.1.0` from the committed real run, so a new draft
gets the next version (`0.1.1`). Model turns vary from run to run.

**3. Approve the draft.** A person signs off, and lowers the search click to read-only
(discovery can't tell a query from a change, so it records clicks as reversible). Approval
is refused unless the verification replay succeeded.

```bash
uv run cua approve corebank.member.lookup_balance 0.1.1 --reviewer "Your Name" \
  --read-only click_search_button
```
```
approved corebank.member.lookup_balance@0.1.1 (risk read_only) by Your Name
```

**4. Replay with params: happy path.** No LLM from here on.

```bash
uv run cua replay corebank.member.lookup_balance --params '{"member_id": "12345"}'
```
```json
{
  "capability_version": "0.1.1",
  "recoveries": [], "interventions": [], "drift": [], "committed_steps": [],
  "kind": "success",
  "outputs": {"member_name": "Jane Q. Sample", "share_savings_balance": "4210.37"}
}
```

`share_savings_balance` is typed `money`: parsed from `$4,210.37` into a decimal.

**5. A business outcome.** An unknown member is a result the caller handles, not an error
(exit code 1).

```bash
uv run cua replay corebank.member.lookup_balance --params '{"member_id": "99999"}'
```
```json
{
  "kind": "business_outcome",
  "code": "no_results",
  "message": "Search ran but no member exists with the given member number; no name or balance can be returned."
}
```

Also try `"34567"` (`access_denied`) or `"12a"` (`failure` / `invalid_input`, rejected
before the browser is touched).

**6. Recovery from an injected fault.** Make the next search show a dismissable system
notice, then replay: the engine recognises the screen from the app's state library, clicks
through it, and carries on.

```bash
curl -X PUT localhost:8001/__admin/faults -H 'content-type: application/json' \
  -d '{"broadcast_notices": 1}'
uv run cua replay corebank.member.lookup_balance --params '{"member_id": "12345"}'
```
```json
{
  "recoveries": [
    {"step_id": "click_search_button", "state": "system_notice", "recovery": "click", "attempt": 1}
  ],
  "kind": "success"
}
```

`{"fatal_errors": 1}` instead gives `failure` / `app_error` with a screenshot in the
evidence. Recovery from a transient 503 and from an expired session is recorded in
[evidence/recorded/](evidence/README.md).

**7. Escalation and handoff to a human.** Member 23456 has a MEMBER ALERT that a person must
read. The run pauses on the live session and asks an operator.

```bash
uv run cua replay corebank.member.lookup_balance --params '{"member_id": "23456"}' \
  --operator console
```
```
operator console: http://127.0.0.1:8765 (hand-offs happen in the browser window)
```

Open the console, enter your name, click **Acknowledge** in the teller window, then
**Hand back (resume)** in the console. The engine takes the session back and finishes:

```json
{
  "recoveries": [
    {"step_id": "click_search_button", "state": "member_alert", "recovery": "escalate", "attempt": 1}
  ],
  "interventions": [{
    "operator": "kingsley",
    "actions": [{"kind": "click", "target_description": "button \"Acknowledge\""}],
    "resolution": "resumed"
  }],
  "kind": "success",
  "outputs": {"member_name": "Robert T. Example", "share_savings_balance": "815.00"}
}
```

Without `--operator console` the run is unattended: it fails closed and aborts at the alert.

## Running without live services

Nothing except step 2 needs a model key, and nothing needs a network service beyond the
local mock app.

- **Tests** (`uv run pytest`) drive discovery with a `ScriptedPlanner` in place of the
  model, so the loop, recorder and verification are tested with no key. Tests that need the
  app run against mock-bank servers the test session starts itself.
- **Demo steps 4–7** run against the approved artifact checked into
  `catalog/corebank/capabilities/` (`0.1.0`, from the real discovery run). Skip steps 2–3
  and they report `"capability_version": "0.1.0"`.
- **`uv run python scripts/record_evidence.py`** starts its own mock banks and re-records
  every run that needs neither a model nor a human (happy path, not found, three recoveries,
  fatal failure, tenant drift, rejected approval) into `evidence/recorded/`, checking each
  result. It takes about 30 s.
- **The real runs are committed**: both discovery attempts (the first was caught by the
  verification gate), the approval, and the console handoff with screenshots. See the
  [evidence index](evidence/README.md).

## Target app: CoreOne Teller

`src/mock_bank` is a deliberately legacy mock of a vendor core-banking product: framesets,
table layouts, labels not tied to inputs, cryptic field names, no test ids. Two tenant
variants run the same product with different branding, labels, version and table layout
(`--variant pinnacle` or `riverbend`). Sign on with `teller1` / `demo-only-password` (a fake
app with a fake credential).

| Member | Behaviour |
|---|---|
| `12345` | Happy path (Share Savings $4,210.37) |
| `23456` | MEMBER ALERT interstitial (needs a human) |
| `34567` | Access denied (restricted account) |
| `99999` | Not found |
| `12a` | Invalid member number |

Injected faults are a test harness only, outside the agent's allowlist:

```bash
curl -X PUT localhost:8001/__admin/faults -H 'content-type: application/json' \
  -d '{"latency_ms": 0, "transient_failures": 1, "fatal_errors": 0, "broadcast_notices": 1, "session_ttl_s": 900}'
curl -X POST localhost:8001/__admin/expire-sessions
curl -X POST localhost:8001/__admin/reset
```

Faults are one-shot counters consumed by the next matching page load, including the one
that follows sign-on, so inject one at a time.

## Tests & contracts

```bash
uv run pytest            # 158 tests, about 3 min (real Chromium against the mock bank)
uv run lint-imports      # 8 architectural contracts
uv run ruff check .
```

The contracts, from `pyproject.toml`:

1. schema is pure: depends on nothing else in cua, no browser/LLM
2. only the session touches the surface
3. replay is deterministic: no LLM
4. discovery and replay are independent drivers
5. surface knows nothing about drivers, policy or control
6. policy is pure decisions: no I/O, no surface, no LLM
7. control and evidence know nothing about the surface or drivers
8. the engine never imports the target app

## Repo layout

```
catalog/corebank/
  app.json                  state library: screens, interstitials, fatal pages
  policy.json               guardrails for this app
  capabilities/             artifacts (+ verification records), draft or approved
goals/                      discovery goals: what to find, typed inputs and outputs
src/cua/                    the engine (modules above)
src/mock_bank/              the target app
scripts/record_evidence.py  reproducible evidence runs
evidence/                   committed runs, indexed in evidence/README.md
tests/                      pytest suite; tests/fixtures/ holds hand-written artifacts
REPORT.md                   design decisions and trade-offs
```
