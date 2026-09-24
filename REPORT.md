# Design Report

An LLM discovers a flow in a legacy UI once; the flow becomes a typed, versioned capability;
a person approves it; from then on it replays without an LLM, with typed inputs and outputs,
an explicit error taxonomy, guardrails on every action, and a handoff to a human on the live
session when a screen needs one. Bank staff do all of this from a browser workbench. The
target is CoreOne Teller, a deliberately hostile mock core-banking app I built (framesets,
table layouts, labels not tied to inputs, cryptic field names, no ids), with two tenant
variants and injectable faults.

| From the real runs (`claude-opus-5`, all in [evidence/](evidence/README.md)) | |
|---|---|
| Real discovery runs | 7: member lookup ×3, sub-account form ×2, savings account number ×1 (from a sentence, in the workbench), identity check ×1 (left as a draft to review) |
| Cost | lookup 5 turns, ~30 s, ~$0.10; form 9 turns, ~55 s; tokens per run in the evidence index |
| Drafts stopped before approval | 2 of 7, both caught: a mis-mapped outcome, and a member's name inside a fallback locator |
| Discovery stability | lookup attempts 2 and 3 produced structurally identical artifacts |
| Second tenant | both capabilities pass on Riverbend with unchanged base artifacts plus an overlay (4 of 4 and 2 of 8 targets replaced) |
| Replay | no LLM; 5 × 10 unattended replays across capabilities and tenants, all `stable`; p50 2.8 s (lookup), 4.5 s (form) |
| Tests | 254 (real Chromium against the mock bank), 9 import-linter contracts, mypy `--strict` clean |

## 1. Architecture

```
   goal ─► discovery (LLM loop + recorder) ─► draft ─► verification ×2 ─► approval ─► stability ×10
                         │                                      │                  │  (clears unattended use)
                         ▼                                      ▼                  ▼
         GuardedSession: policy.check → lease.check → surface.act → evidence  ◄── replay
                         │                 │
                      surface          control ◄──► workbench (Needs you)

   front ends: operator workbench (bank staff) · CLI (engineers, scripts) ─► cua.workflows
```

Two drivers, **discovery** and **replay**, sit on one chokepoint, `GuardedSession`: every
action, whoever issues it, passes the policy, the control lease and the evidence log before it
reaches the surface. The drivers never import each other; the artifact schema is all they
share. Nine import-linter contracts hold these boundaries as the code changes (only the
session touches the surface; replay can't import an LLM client; schema, policy and clearance
are pure; the engine never imports the target app).

Key decisions:

- **Perceive through the accessibility tree plus visible text**, not DOM selectors or
  pixels. Legacy markup has no ids but still exposes roles, names and text; the same element
  model maps onto UIA/AX trees on desktop (§4); it is cheaper and more deterministic than
  screenshots. The trade-off: a surface without an accessibility tree needs another adapter.
- **The LLM never runs the work.** It discovers (a manual tool loop, one call per turn so it
  sees each result, with strict tools, adaptive thinking, prompt caching and context
  editing), proposes contracts, and in the chat picks which saved automation a request means;
  replay never uses it. It never sees credentials, types `{{inputs.x}}` placeholders instead
  of example values, and never sees member data typed into the chat (§6).
- **The artifact is built from what executed, not from what the model says.** The recorder
  keeps each action that succeeded, with the locator the session synthesized and verified and
  the risk the policy applied; the model only picks the steps and names the outcomes.
- **Nothing the model produced runs before a person signs off**, and nothing runs unattended
  without measured stability (§6). Approved versions are immutable.
- **One process, synchronous**, every seam a protocol (`Surface`, `ControlPort`, `Planner`);
  the cost is one live session per process (§7).

**The people in the loop.** A CLI serves engineers, not bank staff, so the actions live in
`cua.workflows` behind two thin front ends, and staff use the **operator workbench**, which
opens on a chat: *what do you need done?* A request a saved automation covers comes back as
a card with the inputs filled in, which runs only when the person presses Run; one that
nothing saved fully covers becomes an offer to set up a new one. For that, an *employee*
describes the work in their own words; Claude proposes the typed contract (one
structured-output call) with questions for what it had to guess, and the person corrects it
and gives example values before discovery runs, narrated live. A *reviewer* approves a draft
as numbered steps in plain words ("Run the member search · button "Search" · may change
data") with a screenshot after each from the checks, never as JSON: an approval gate is only
as good as what the approver can read. Results say what happened and what to do next, and a
run started there is attended, so a handoff comes to that person. *Callers* use the CLI or
the functions and need stability clearance to run unattended. Evidence: `evidence/workbench/`.

## 2. Artifact schema

A capability (`catalog/corebank/capabilities/*.json`, pydantic, JSON on disk) has three parts:

- **Contract**, for the caller: typed `inputs` (with `sensitivity`, `pattern`, `enum`), typed
  `outputs` (`money` is parsed from `$4,210.37` into a decimal), business `outcomes` mapped to
  screen states, and an overall `risk`.
- **Execution**, for replay: an `entry`, named `targets`, and ordered `steps`, each with an
  `intent`, an action (`navigate`, `click`, `fill`, `select`, `press`, `extract`), a declared
  `risk` and an `expect` checkpoint, plus a `success` condition.
- **Governance**: semver `version`, `status` (draft, approved, deprecated), `provenance`
  (discovery run, model, reviewer) and `app` (`app_id`, `surface`, `compat`).

```json
"member_id_field": {
  "frame": [{"name": "content"}],
  "strategies": [
    {"by": "near", "text": "Member ID", "direction": "right", "role": "textbox"},
    {"by": "css", "selector": "input[name=\"mbrno\"]"}],
  "fingerprint": {"role": "textbox", "tag": "input"}}
```

Why this shape:

- **Targets are named and live apart from steps**, so a reviewer reads `click search_button`
  and a tenant can override one target by name (§4).
- **A target is a frame path, ranked strategies and a fingerprint.** Strategies are what an
  operator perceives: role and name, label, nearby text, table cell (row × column), CSS last.
  The first that matches exactly one element wins and the fingerprint must agree; several
  matches is `target_ambiguous`, never a silent pick; winning with a fallback is drift.
- **Screens live in a per-app state library** (`app.json`: every known screen, interstitial
  with its recovery, and fatal page, by text predicates with precedence), not in capabilities,
  which only say which states are business outcomes for them. It is hand-written per vendor
  product and shared by every tenant; discovery maps outcomes onto it but doesn't create it.
- **The schema rejects inconsistent artifacts:** unused or undeclared inputs and targets,
  outputs never extracted, secrets as inputs or outputs, and outcomes on screens the flow
  passes through. That last rule came from real lookup attempt 1: the model mapped
  `system_error` onto the search form, and verification caught it.
- **An artifact never contains** coordinates, handles, secrets or concrete values; the store
  refuses one containing any value the run knew to be sensitive.

The trade-off: steps are a straight line plus the classifier, with no branching. A conditional
flow is two capabilities or a recovery in the state library; replay stays deterministic and a
capability stays reviewable in one read.

## 3. Determinism & error handling

After every step, replay classifies the screen against the state library, the single owner of
"what happened", and polls until the checkpoint state appears or its timeout passes; there are
no fixed sleeps. Every result is one of:

| Kind | Meaning | Example |
|---|---|---|
| `success` | outputs, typed | `recorded/01`, `08a` |
| `business_outcome` | a legitimate result the caller handles, with a `code` | not found (`02`), deposit rejected (`08b`) |
| `failure` | `category`, `step_id`, `expected`, `observed`, `retryable` | `app_error` (`04`), `target_not_found` (`05`), `timeout` (`07b`), `invalid_input`, `checkpoint_mismatch`, `recovery_exhausted`, `policy_denied`, `output_invalid` |
| `aborted` | a human or the approval gate stopped it | rejected approval (`06`) |

Every result also carries `recoveries`, `interventions`, `drift`, `committed_steps` and a
link to its evidence.

**Recoverable conditions are handled in-run and listed, not returned.** Each interstitial
declares its recovery: `click` (a broadcast notice, `03c`), `retry` (a 503, `03b`),
`reauthenticate` (an expired session, `03a`) or `escalate` (a member alert, §5), bounded per
state, then `recovery_exhausted`. Slowness has no screen, so it is a budget: 10 s per action,
and a timeout before anything is committed restarts the run once (`07a` recovered; `07b`
still slow, `failure/timeout`, retryable).

**Replay knows what it may have committed.** A step counts as committed if the artifact
declares it more than read-only or the policy sees an irreversible control. Before the first
commit, restarting is safe and failures are retryable; after it, the engine never restarts,
and a timeout, unknown screen or checkpoint mismatch goes to a human. The rule is
conservative: in `08b`, *Continue* submitted a form the app rejected and still counts.

**Locators are verified twice before approval.** Verification replays the draft with the
goal's examples and with a second member, checks every strategy of every target, and drops
any that didn't find the same element both times. That came from real sub-account attempt 1:
a fallback for *Continue* was anchored on "OPEN SUB-ACCOUNT — <member name>", which would
never match another member and put a name in the artifact (§6). Attempt 2 dropped it.

**Drift is reported, never guessed.** The Pinnacle-recorded lookup on Riverbend (`05`)
resolves the member field through its CSS fallback (drift) and then fails explicitly at
`search_button`, labelled "Find" there. An overlay (§4) makes it pass (`05b`).

## 4. Heterogeneity & multi-tenant

**The surface seam.** Everything above `cua.surface` speaks `Target`s and `Observation`s; an
adapter implements `observe`, `resolve`, `act`, `audit` and `pin`/`synthesize_target`.

- **Legacy web** is built (Playwright): framesets are a `frame` path; unlabelled inputs
  resolve by nearby text or table position.
- **Desktop** (Win32, WinForms, Java Swing via UIA or AX): the tree has roles, names and
  bounds, so the same strategies apply; the frame path becomes a window path.
- **Pixels only** (Citrix, terminal emulators): a vision adapter turning OCR and layout into
  the same element tree, so `near` and `table_cell` still work.

Artifacts and state libraries don't change across these; only the adapter does.

**Reuse across tenants.** Institutions run the same vendor product with different labels,
versions and layout, which the two variants imitate. Three layers, each reviewed separately:

1. **Vendor app model** (state library, sign-on, policy), keyed by product and version. Its
   predicates use vendor-constant text, so they held on Riverbend unchanged.
2. **Base capability**, recorded once on one tenant; tenant URLs are runtime configuration.
3. **Tenant overlay** (`catalog/corebank/tenants/riverbend/`): replaces named targets of one
   approved base version and nothing else, so it changes where a flow acts but never what it
   does or promises; reviewing one means reviewing locators. It replaces whole targets,
   fingerprint included, and is pinned to one base version.

`--tenant riverbend` runs the base with the tenant's approved overlay as `0.1.0+riverbend`
(SemVer build metadata), so every result names what ran; the replay engine didn't change. A
draft overlay is an error, never skipped: the base would act on the wrong elements.
Riverbend needed all four lookup targets and two of eight form targets replaced (the search
screen). I wrote both overlays by hand from `05`; each passed the same gate as a draft
(`cua verify-overlay`: two members, pruning), and `05b`/`05c` pass with no drift.

**Managing drift.** Every replay reports fallbacks, so per-tenant drift shows before anything
fails. In production a `target_not_found` would start discovery scoped to that one target,
producing an overlay draft, not a re-recording; that isn't built. Overrides keyed by screen
rather than capability would remove the repetition between the two overlays, and a `compat`
check against the product banner would stop a run on an unverified version (also not built).

## 5. Escalation & handoff

**Detecting "stuck".** A person is brought in when the state library marks a screen
`escalate` (the member alert, never auto-dismissed); when, after a commit, the screen is
unknown, a checkpoint isn't reached or an action times out; when an irreversible action needs
approval; and in discovery when the model calls `request_help`. Before any commit, stuck is a
retryable failure: cheaper than a person.

**Control model.** `cua.control` holds a lease on the live session, identified by an epoch.
Acquiring it invalidates every earlier token, and the session checks its token before
resolving a target and again before acting, so the agent can't race an operator. A request
carries the run, capability, step, redacted reason and a screenshot. Three ways in: the engine
escalates; an operator **pauses** (the agent hands over at its next action and continues
after); or **takes over** (the run ends `aborted`).

**The handoff.** The run pauses on the same headed browser session. The request appears
under *Needs you* in the workbench (or the per-run console); the person acts in that browser
window, and an injected script records what they did (clicks by accessible name, fills by
length only). On *hand back* the engine re-acquires the lease under a new epoch,
reclassifies the screen and continues; the result lists the intervention. Unattended runs
fail closed: approvals are rejected, handoffs abort.

Evidence: run `20260923T194339Z-f6c82a` and the two console screenshots (member 23456's
alert acknowledged in the live window, run finished with outputs); the workbench tour shows
a run arriving under *Needs you*. Handing back without fixing the screen escalates again,
and after two attempts fails `recovery_exhausted`. Mocked: no authentication, a typed name,
one local session; production needs an intervention queue with SLAs, SSO, and the browser
streamed to the operator.

## 6. Safety

**Hard limits, for every actor and mode:** an origin allowlist (per tenant, at runtime); a
path allowlist and denylist per app (`/signoff` and `/__admin` are outside); an action-kind
allowlist (CoreOne omits `press`: a key press can submit a form without a named button whose
risk the policy could judge); only the runtime fills password fields; at most 200 actions and
1 irreversible action per run. Discovery isn't offered disallowed tools, and the session
refuses them anyway.

**Risk is never taken on trust.** The policy infers it from the element (`confirm`, `submit`,
`post`, `transfer`, `pay`, `delete`… are irreversible), the artifact declares it, and the
higher wins. A reviewer may only lower reversible steps to read-only. **Irreversible actions
need a person's approval every time** and are denied outright for unapproved capabilities: I
chose approval over blocking because banks need these actions done, but at this maturity none
should happen unattended. Discovery stops before commits: the sub-account flow ends on the
review screen, and the policy refused *Confirm* when a scripted model tried it.

**Unattended use is measured, not granted.** `cua stability` replays an approved capability
ten times unattended over two input sets and saves a report beside it (verdict, success
rate, consistency, drift, recoveries, durations), re-derived from its runs on load so it
can't claim more than they show. Unattended replay needs the app's thresholds (10 runs, all
successful, no drift, within 30 days) for that exact version, so a new base or overlay starts
uncleared; otherwise it exits 2 with the reason, and attended runs need approval alone.
Recoveries are reported, not scored. Irreversible capabilities are never measured or cleared.
It is a run-level check, not a policy rule, since the policy doesn't know whether anyone is
watching. Every real measurement is 10/10, which says little about a deterministic mock; the
tests show the gate refusing flaky, broken, drifting, short and stale reports.

**Secrets and data.** Credentials are `env:` references resolved at the last moment and never
reach the model, artifacts or logs. Redaction is value-based at every sink: sensitive inputs,
extracted outputs and secrets are registered as the run goes and replaced by placeholders
(`«inputs.member_id»`), with SSN and card patterns as a backstop. Nothing typed by staff
reaches the model with member data in it: the chat takes numbers, amounts and emails out
before the router is called («v1») and fills them back into the card locally, and the
contract proposal refuses text that looks like member data. Names typed into the chat are
not detected; they still only become run inputs after the person checks the card.

**Found in real runs and tests, and fixed:** the catalog stored the unredacted verification
result (now kind and run ids only); a member's name reached an artifact through a locator
anchor (§3; now pruned, and the store refuses sensitive values); and per-step screenshots
written as the run went left the name in the snapshot text, because the screen after
"Search" shows it before the step that reads it (now written at the end of the run).

**Limits.** Value-based redaction can't know data nobody declared: screenshots show whatever
was on screen, and discovery's append-only log keeps locators as synthesized, before pruning.
The model sees screens, so discovery belongs on a sandbox tenant with synthetic members and a
zero-retention provider. Irreversibility inference is a name heuristic ("OK" can post a
transaction), which is what `irreversible_patterns` and review are for. What a person does in
a handoff is recorded but not gated.

## 7. Cuts

Deliberately left out:

- **Scoped discovery for overlays** (§4): overlays are hand-written, then verified and
  approved; no `compat` check.
- **Desktop and vision adapters**: the seam exists; no second adapter.
- **A production workbench**: no login or roles (anyone can approve; the name is typed),
  localhost, one job at a time, the handoff window on the same machine.
- **Scale**: one live session per process, synchronous; no queue or workers.
- **Verification of irreversible flows**: verification must not commit, so a flow through
  *Confirm* can't be proven by replay; opening the account is a hand-written fixture
  exercised only in tests and `06`.
- **LLM limits**: a turn budget per discovery, no cost or rate limits; one provider behind
  the `Planner` protocol.
- **On-screen data detection**: undeclared data in screenshots (§6).
- **A conversation**: the chat routes one request at a time; there is no memory across
  messages. Invoking saved automations by name with typed inputs is close to the brief's
  agent-facing stretch goal; I don't claim it as a third.

Stretch goals, two as the brief suggests: cross-tenant reuse with per-variant overrides (§4),
and confidence & approval, with a measured stability score clearing approved capabilities for
unattended replay (§6), as a batch rather than a rolling window over real runs.

Next, in order: drift-triggered, scoped discovery that drafts overlays; approved capabilities
as a typed tool catalog for agents; entity detection on screens before evidence is written;
sandbox-tenant verification for irreversible flows; stability from a rolling window of real
replays, revoked on drift; then queue-backed workers with one session each.
