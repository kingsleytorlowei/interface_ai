# Computer-Use Automation System

An LLM discovers a workflow in a legacy UI **once**; the result is recorded as a typed,
versioned **capability artifact**; after a person approves it, it is **replayed
deterministically, without an LLM**, with typed inputs and outputs, explicit error handling,
guardrails on every action, and a handoff to a person on the live session when a screen
needs one.

Bank staff work in the **operator workbench**, which opens on one question: *what do you need
done?* Ask in plain words and it finds the saved automation that does it, fills in what you
gave and runs it once you've checked; ask for something new and it sets one up, which a
reviewer approves after reading it as steps in their own words. The command line is for
engineers and scripted runs.

Design write-up: [REPORT.md](REPORT.md) · Evidence: [evidence/](evidence/README.md) ·
Workbench screenshots: [evidence/workbench/](evidence/workbench/)

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                # install dependencies into .venv
uv run playwright install chromium     # browser for the web surface
cp .env.example .env                   # then set ANTHROPIC_API_KEY (discovery only)
```

`.env` also holds the mock app's teller credentials, resolved at run time; it is git-ignored.
Only discovering new automations needs the key: reviewing, approving, running, and the
command-line demo from step 4 on work without it.

## Start here (10 minutes, no API key needed)

```bash
uv run python scripts/demo.py     # both mock banks and the workbench; opens your browser
```

1. **Ask.** Type *what's the savings balance for member 45678?* It finds the saved automation,
   fills in 45678 (the number never leaves your computer; the assistant sees a placeholder)
   and runs when you press **Run**; the result prints as a receipt. Without a key it matches
   by words instead.
2. **Review a draft.** Under *Saved automations*, open **Confirm a member's identity**: a real
   discovery left waiting for you. Read its steps and what each one saw, tick the search step
   as "only looks things up", and **Approve** it (or **Reject** it with a reason).
3. **Run it.** On its page, **Run** with member `12345`. A teller window opens (that's the
   automation's live session); the result comes back in plain words.
4. **Be the person in the loop.** Ask *look up member 23456* and run it. It stops at a member
   alert and appears under **Needs you**: click **Acknowledge** in the teller window, then
   **Hand back (resume)**, and the run finishes.
5. **Read [REPORT.md](REPORT.md)** for the design and trade-offs. With a key in `.env`,
   asking for something nothing saved does sets up a new draft (about a minute and $0.10).

No time to run it? [evidence/workbench/](evidence/workbench/) has every screen, from a real
run.

## The operator workbench

```bash
uv run python -m mock_bank --variant pinnacle --port 8001     # the target app, own terminal
uv run cua console                                            # http://127.0.0.1:8765
```

| Screen | What a person does there |
|---|---|
| **Home** | Asks for what they need. A saved automation that does it comes back as a card with the inputs filled in, to check and **Run**; its result prints as a receipt. Something new comes back as an offer to set it up. Values in the message are taken out before anything is sent. Saved automations are listed underneath. |
| **New automation** | Describes the work in their own words. Claude proposes what it needs and gives back and asks about what it had to guess; the person corrects it and gives two example values per input. Discovery then runs with its steps narrated, and both checking replays follow. |
| **Review** | Reads a draft as numbered steps (what, where in words, what it can change, a screenshot after each), then **Approves** it or **Rejects** it with a reason. Approved ones show stability per institution and can be measured. |
| **Run** | Fills a form built from the inputs; the result says what happened and what to do next. |
| **Needs you** | Answers approvals and handoffs from runs started here. |

Add `--tenant riverbend=http://127.0.0.1:8002` for the second institution. The automation's
browser window opens on screen, because a handoff happens in it. One job runs at a time.
[`scripts/workbench_tour.py`](scripts/workbench_tour.py) drives every screen with the real
model; its screenshots are in [evidence/workbench/](evidence/workbench/).

## Demo path (command line)

Each replay prints a JSON `RunResult` (trimmed below) and writes a redacted evidence
directory under `evidence/<run_id>/`. The exit code mirrors the result: `0` success,
`1` failure, `3` business outcome, `4` aborted, `2` usage error (including "not cleared for
unattended replay").

**1. Start the target app**: `uv run python -m mock_bank --variant pinnacle --port 8001`.

**2. Discover with the LLM** (needs the key; ~30 s, ~$0.10). Discovery records a draft,
verifies it by replaying it without the LLM with two members, and drops any fallback that
didn't hold for both.

```bash
uv run cua discover goals/corebank.member.lookup_balance.yaml
```
```
discovery: recorded (flow recorded) in 5 turns; usage {...}; evidence evidence/<run_id>
  review: step click_search_button is recorded as reversible; lower it to read_only at review if it only queries
verification 1: success; evidence evidence/<run_id>
verification 2: success; evidence evidence/<run_id>
draft saved: catalog/corebank/capabilities/corebank.member.lookup_balance@0.1.1.json
```

**3. Approve, then measure stability.** A person signs off (lowering the search click to
read-only); running with nobody watching also needs ten clean unattended runs. Without the
key, skip steps 2–3: the approved `0.1.0` and its stability report are already committed.

```bash
uv run cua approve corebank.member.lookup_balance 0.1.1 --reviewer "Your Name" \
  --read-only click_search_button
uv run cua stability corebank.member.lookup_balance \
  --params '{"member_id": "12345"}' --params '{"member_id": "45678"}'
```
```
stable: 100% success over 10 runs, 0 with drift, 0 with recoveries, p50 2.82s; saved ...
cleared for unattended replay
```

**4. Replay: happy path.** No LLM from here on.

```bash
uv run cua replay corebank.member.lookup_balance --params '{"member_id": "12345"}'
```
```json
{"capability_version": "0.1.1", "drift": [], "committed_steps": [], "kind": "success",
 "outputs": {"member_name": "Jane Q. Sample", "share_savings_balance": "4210.37"}}
```

**5. A business outcome** is a result, not an error (exit 3): `--params '{"member_id":
"99999"}'` gives `business_outcome` / `no_results`. `"34567"` gives `access_denied`; `"12a"`
is `failure` / `invalid_input`, rejected before the browser is touched.

**6. Recovery from an injected fault.** Make the next search show a notice; the engine
recognises it from the app's state library, clicks through and carries on.

```bash
curl -X PUT localhost:8001/__admin/faults -H 'content-type: application/json' \
  -d '{"broadcast_notices": 1}'
uv run cua replay corebank.member.lookup_balance --params '{"member_id": "12345"}'
```
```json
{"recoveries": [{"step_id": "click_search_button", "state": "system_notice", "recovery": "click", "attempt": 1}],
 "kind": "success"}
```

The other recoveries (503, expired session, hung load) and a clean `timeout` are recorded in
[evidence/recorded/](evidence/README.md); a 503 or stall injected here is consumed by the page
that loads right after sign-on, so the recorder injects them after it.

**7. Handoff to a person.** Member 23456 has a MEMBER ALERT that a person must read.

```bash
uv run cua replay corebank.member.lookup_balance --params '{"member_id": "23456"}' \
  --operator console
```

Open the console it prints, click **Acknowledge** in the teller window, then **Hand back
(resume)**. The run finishes with `interventions: [{"operator": ..., "actions": [{"kind":
"click", "target_description": "button \"Acknowledge\""}], "resolution": "resumed"}]`.
Unattended, it fails closed and aborts at the alert.

**8. A form flow**: open a sub-account and stop on the review screen, before the
irreversible *Confirm*, with inputs discovery never used.

```bash
uv run cua replay corebank.subaccount.prepare \
  --params '{"member_id": "45678", "account_type": "Share Certificate", "initial_deposit": "500"}'
```
```json
{"committed_steps": ["click_continue_button"], "kind": "success",
 "outputs": {"account_type": "Share Certificate", "initial_deposit": "500.00"}}
```

*Continue* counts as committed: nothing is created yet, but a restart would submit the form
again. A deposit of `"1.00"` gives `business_outcome` / `subaccount_values_rejected`.

**9. Another tenant.** Riverbend runs the same product with other labels. As recorded, the
lookup fails there explicitly (`target_not_found` at `click_search_button`, with drift
reported); with `--tenant riverbend` it runs with an approved overlay that replaces only the
targets that differ.

```bash
uv run python -m mock_bank --variant riverbend --port 8002     # own terminal
uv run cua replay corebank.member.lookup_balance --params '{"member_id": "12345"}' \
  --base-url http://127.0.0.1:8002 --tenant riverbend
```
```json
{"capability_version": "0.1.0+riverbend", "drift": [], "kind": "success", ...}
```

## Evidence without live services

- **Tests** drive discovery with a `ScriptedPlanner` in place of the model and run against
  mock banks the test session starts itself.
- **`uv run python scripts/record_evidence.py`** re-records every run that needs neither a
  model nor a person (14 scenarios: happy path, outcomes, recoveries, failures, tenant drift
  and overlays, rejected approval, the form flow) into `evidence/recorded/`, checking each.
- **The real runs are committed**: seven discovery runs (two caught before approval, one left
  as a draft to review), the handoff, stability measurements and the workbench tour. See the
  [evidence index](evidence/README.md).

## Target app: CoreOne Teller

`src/mock_bank` is a deliberately legacy mock of a vendor core-banking product: framesets,
table layouts, labels not tied to inputs, cryptic field names, no test ids, with two tenant
variants (`--variant pinnacle` or `riverbend`). Sign on with `teller1` / `demo-only-password`.

| Member | Behaviour |
|---|---|
| `12345`, `45678` | Happy path |
| `23456` | MEMBER ALERT (needs a person) |
| `34567` | Access denied |
| `99999` | Not found |

Faults (a test harness, outside the agent's allowlist; each `PUT /__admin/faults` replaces the
set): `broadcast_notices`, `fatal_errors`, `transient_failures`, `stalled_loads`, `latency_ms`,
`session_ttl_s`; plus `POST /__admin/expire-sessions` and `POST /__admin/reset`.

## Tests & contracts

```bash
uv run pytest            # 255 tests, about 4 min (real Chromium against the mock bank)
uv run lint-imports      # 9 architectural contracts (pyproject.toml)
uv run mypy              # strict, with the pydantic plugin
uv run ruff check .
```

The contracts: schema, policy and stability clearance are pure; only the session touches the
surface; replay can't import an LLM client; discovery and replay are independent; the surface,
control and evidence know nothing about the drivers; the engine never imports the target app.

## Layout

| Path | What |
|---|---|
| `src/cua/schema` | Pure data contracts: capability, overlay, targets, states, results |
| `src/cua/surface` | Perception and action port, and its Playwright web adapter |
| `src/cua/session.py` | `GuardedSession`: policy → lease → surface → evidence, for every action |
| `src/cua/policy.py`, `stability.py` | Guardrails and redaction; clearance for unattended use |
| `src/cua/control.py` | The lease on a live session; approval and intervention requests |
| `src/cua/discovery` | The LLM loop, recorder and contract proposal: the only place an LLM is used |
| `src/cua/replay` | Deterministic execution, classification, recovery, typed results |
| `src/cua/workflows.py` | The actions both front ends share |
| `src/cua/operator` | The workbench and the per-run console |
| `src/cua/store.py`, `evidence.py`, `cli.py` | Catalog, evidence directories, command line |
| `catalog/corebank/` | State library, policy, capabilities, tenant overlays, verification and stability records |
| `goals/`, `scripts/`, `evidence/`, `tests/` | Discovery goals, evidence and tour scripts, committed runs, test suite |
| `src/mock_bank` | The target app |
