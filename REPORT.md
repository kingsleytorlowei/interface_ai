# Design Report

An LLM discovers a flow in a legacy UI once; the flow becomes a typed, versioned capability;
a person approves it; from then on it replays without an LLM, with typed inputs and outputs,
an explicit error taxonomy, guardrails on every action, and a handoff to a human on the live
session when a screen needs one. The target is CoreOne Teller, a deliberately hostile mock
core-banking app I built (framesets, table layouts, labels not tied to inputs, cryptic field
names, no ids, results rendered at a POST), with two tenant variants and injectable faults.

| From the real runs (`claude-opus-5`, all in [evidence/](evidence/README.md)) | |
|---|---|
| Real discovery runs | 5: member lookup ×3, sub-account form flow ×2 |
| Lookup | 5 turns, ~30 s, ~$0.10 (10 input / 21,472 cache-read / 8,576 cache-write / 1,284 output tokens) |
| Sub-account form flow | 9 turns, ~55 s (18 / 59,866 / 12,646 / 1,712) |
| Drafts stopped before approval | 2 of 5, both caught by verification or review: a mis-mapped outcome, and a member's name inside a fallback locator |
| Discovery stability | lookup attempts 2 and 3 produced structurally identical artifacts |
| Second tenant | both capabilities pass on Riverbend with unchanged base artifacts plus a tenant overlay: all 4 lookup targets and 2 of 8 form targets replaced |
| Replay | no LLM; 4 × 10 unattended replays (2 capabilities × 2 tenants) all `stable`, no drift; p50 2.8 s (lookup), 4.5 s (form), including sign-on |
| Tests | 216 (real Chromium against the mock bank), 9 import-linter contracts, mypy `--strict` clean |

## 1. Architecture

```
   goal ─► discovery (LLM loop + recorder) ─► draft ─► verification ×2 ─► approval ─► stability ×10
                         │                                      │                  │   (clears unattended use)
                         ▼                                      ▼                  ▼
         GuardedSession: policy.check → lease.check → surface.act → evidence  ◄── replay ◄─ agent
                         │                 │
                      surface          control ◄──► operator console
```

Two drivers, **discovery** and **replay**, sit on one chokepoint, `GuardedSession`: every
action, whoever issues it, passes the policy, the control lease and the evidence log before it
reaches the surface. The drivers never import each other; the artifact schema is all they
share. Nine import-linter contracts enforce the boundaries (only the session touches the
surface; replay cannot import an LLM client; the schema, policy and clearance check are
pure; the engine never imports the target app), so they hold as the code changes.

Key decisions:

- **Perceive through the accessibility tree plus visible text**, not DOM selectors or
  pixels. Legacy markup has no ids but still exposes roles, names and text; the same element
  model maps onto UIA/AX trees on desktop (§4); and it is cheaper and more deterministic than
  screenshots. The trade-off: a surface with no accessibility tree needs another adapter.
- **The LLM only discovers.** Claude runs in a manual tool loop, one tool call per turn so it
  sees each result, strict tool schemas, adaptive thinking, prompt caching and context editing
  that clears old screens. It never sees credentials (the runtime signs on) and types
  `{{inputs.x}}` placeholders instead of example values.
- **The artifact is built from what executed, not from what the model says.** The recorder
  records each action that succeeded, with the locator the session synthesized and verified
  and the risk the policy applied. The model only chooses which recorded steps form the flow
  and names the business outcomes.
- **Nothing the model produced runs unattended before a person signs off.** A draft is
  verified by replaying it twice without the LLM (§3); approval is refused without a
  successful verification; approved versions are immutable. Approval then allows attended
  replay; running with nobody watching also needs a measured stability report (§6).
- **One process, synchronous**, with every seam a protocol (`Surface`, `ControlPort`,
  `Planner`); the cost is one live session per process (§7).

## 2. Artifact schema

A capability (`catalog/corebank/capabilities/*.json`, pydantic, JSON on disk) has three parts:

- **Contract**, for the calling agent: typed `inputs` (with `sensitivity`, `pattern`,
  `enum`), typed `outputs` (`money` is parsed from `$4,210.37` into a decimal), `outcomes`
  (business results mapped to screen states), and an overall `risk`.
- **Execution**, for replay: an `entry`, named `targets`, and ordered `steps`, each with an
  `intent`, an action (`navigate`, `click`, `fill`, `select`, `press`, `extract`), a declared
  `risk` and an `expect` checkpoint (the state the screen must reach), plus a `success`
  condition (final state and outputs present).
- **Governance**: semver `version`, `status` (draft, approved, deprecated), `provenance`
  (discovery run, model, reviewer) and `app` (`app_id`, `surface`, `compat` range).

```json
"member_id_field": {
  "frame": [{"name": "content"}],
  "strategies": [
    {"by": "near", "text": "Member ID", "direction": "right", "role": "textbox"},
    {"by": "css", "selector": "input[name=\"mbrno\"]"}],
  "fingerprint": {"role": "textbox", "tag": "input"}}
```

Why this shape:

- **Targets are named and live apart from steps.** A reviewer reads `click search_button`, a
  tenant can override one target by name (§4), and several steps can share one.
- **A target is a frame path, ranked strategies and a fingerprint.** Strategies are what an
  operator perceives: role and name, label, nearby text, table cell (row text × column), and
  CSS only as a last resort. The first strategy that matches exactly one element wins and the
  fingerprint must agree; several matches is `target_ambiguous`, never a silent pick. Winning
  with a fallback is reported as drift.
- **Screens live in a per-app state library, not in capabilities.** `app.json` names every
  known screen, interstitial (with its recovery) and fatal page by text predicates with
  precedence. Capabilities only say which states are business outcomes *for them*
  (`no_results` is a screen to the app, `no_such_member` to the lookup). This library is
  hand-written per vendor product: discovery maps the goal's outcomes onto it but does not
  create it. Error screens are vendor constants, written once and shared by every tenant.
- **The schema rejects inconsistent artifacts:** undeclared or unused inputs and targets,
  outputs never extracted, secrets as inputs or outputs, and outcomes placed on screens the
  flow passes through. That last rule came from real lookup attempt 1: the model mapped
  `system_error` onto the search form, verification ended as a business outcome, and approval
  was refused.
- **An artifact never contains** coordinates, adapter handles, secrets or concrete values,
  and the store refuses one that contains any value the run knew to be sensitive.

The trade-off: steps are a straight line plus the classifier, with no branching or loops. A
conditional flow is two capabilities or a recovery in the state library. That keeps replay
deterministic and a capability reviewable in one read.

## 3. Determinism & error handling

After every step, replay classifies the screen against the state library, which is the single
owner of "what happened", and polls until the step's checkpoint state appears or its timeout
passes. There are no fixed sleeps. Every result is one of:

| Kind | Meaning | Example |
|---|---|---|
| `success` | outputs, typed | `recorded/01`, `08a` |
| `business_outcome` | a legitimate result the caller handles, with a `code` | not found (`02`), deposit rejected (`08b`) |
| `failure` | `category`, `step_id`, `expected`, `observed`, `retryable` | `app_error` (`04`), `target_not_found` (`05`), `timeout` (`07b`), `invalid_input`, `checkpoint_mismatch`, `recovery_exhausted`, `policy_denied`, `output_invalid` |
| `aborted` | a human or the approval gate stopped it | rejected approval (`06`) |

Every result also carries `recoveries`, `interventions`, `drift`, `committed_steps` and a
link to its evidence.

**Recoverable conditions are handled in-run and listed, not returned.** Each interstitial in
the state library declares its recovery: `click` (dismiss a broadcast notice, `03c`), `retry`
(a 503: restart from entry, `03b`), `reauthenticate` (an expired session, `03a`) or
`escalate` (a member alert, §5). Each is bounded per state; past the bound the run fails with
`recovery_exhausted`. Slowness has no screen to classify, so it is a budget instead: every
action gets 10 s, and a timeout before anything is committed restarts the run once (`07a`,
hung load recovered; `07b`, still slow, `failure/timeout`, retryable).

**Replay knows what it may have committed.** A step counts as committed if the artifact
declares it more than read-only or the policy sees an irreversible control. Before the first
commit, restarting is safe and failures are retryable. After it, the engine never restarts: a
timeout, an unrecognised screen or a checkpoint mismatch goes to a human, and failures are
not retryable. The definition is deliberately conservative: in `08b`, *Continue* submitted a
form the app then rejected, and it still appears in `committed_steps`.

**Locators are verified twice before approval.** Discovery synthesizes every strategy that
uniquely identifies the element on the live screen. Verification then replays the draft with
the goal's examples and with a second, different set (another member), checking every
strategy of every target, and drops any that didn't find the same element in both runs. That
rule came from real sub-account attempt 1: a fallback for *Continue* was anchored on the
heading "OPEN SUB-ACCOUNT — <member name>", which could never match another member and put
the member's name in the artifact (§6). Attempt 2 dropped it automatically.

**Drift is reported, never guessed.** Replaying the Pinnacle-recorded lookup on the Riverbend
tenant (`05`) resolves the member field through its CSS fallback (a drift signal) and then
fails explicitly at `search_button`, because Riverbend labels it "Find". A tenant overlay
(§4) is what makes it pass (`05b`).

## 4. Heterogeneity & multi-tenant

**The surface seam.** Everything above `cua.surface` speaks `Target`s and `Observation`s; an
adapter implements `observe`, `resolve`, `act`, `audit`, and `pin`/`synthesize_target` for
discovery. The web adapter is Playwright. The others, by design:

- **Legacy web** is what is built: nested framesets are a `frame` path, and unlabelled inputs
  resolve by nearby text or table position.
- **Desktop** (Win32, WinForms, Java Swing through UIA or AX): the tree already has roles,
  names and bounds, so the same strategies apply; the frame path becomes a window path, and
  `app.surface` selects the adapter.
- **Pixels only** (Citrix, terminal emulators): a vision adapter that turns OCR and layout into
  the same element tree, so `near` and `table_cell` still work; for green screens, the state
  library's text predicates fit naturally.

Artifacts and state libraries don't change across these; only the adapter does.

**Reuse across tenants.** Many institutions run the same vendor product with different
branding, labels, versions and layout, which is what the two variants imitate. Three layers,
each owned and reviewed separately:

1. **Vendor app model**, keyed by product and version range: the state library, sign-on and
   policy. Its predicates use vendor-constant text, so they held on Riverbend unchanged (the
   run passed the search screen's checkpoint there).
2. **Base capability**, recorded once on one tenant; `app.compat` declares the product
   versions it was verified on. Tenant URLs are runtime configuration (`{{env.base_url}}`).
3. **Tenant overlay**: a file per tenant and capability
   (`catalog/corebank/tenants/riverbend/`) that replaces named targets of one approved base
   version, and nothing else. It can't touch steps, risk, inputs, outputs, outcomes or
   states, so it can change where a flow acts but never what it does or promises its caller;
   reviewing one means reviewing locators, not a flow. It replaces whole targets, fingerprint
   included (Riverbend's "Find" button is not a "Search" button with one more strategy), and
   is pinned to an exact base version, so a new base doesn't silently inherit it.

`--tenant riverbend` applies that tenant's approved overlay to the approved base and runs it
as `0.1.0+riverbend` (SemVer build metadata), so every result names what ran; a tenant with
no overlay runs the base. The replay engine didn't change. A draft overlay is an error, never
skipped: running the base where an overlay exists would act on the wrong elements.

Riverbend needed four overrides for the lookup (the member field, which had only resolved
through its CSS fallback; "Find"; "Member Name:"; "Current Bal.") and two of eight for the
sub-account flow, whose form and review screens match. I wrote both by hand from the drift
evidence (`05`); each went through the same gate as a discovered draft (`cua verify-overlay`
replays it on Riverbend with two members and drops strategies that didn't hold both times;
approval needs that). The base artifacts are unchanged, and `05b`/`05c` pass with no drift.

**Managing drift.** Every replay reports which targets fell back to a lower-ranked strategy,
so per-tenant drift is visible before anything fails. In production, a `target_not_found` on
one tenant would start discovery scoped to that target, "find this element here", whose
output is an overlay draft rather than a re-recording; that is not built, and overlays are
hand-written today. The two Riverbend overlays repeat the search screen's targets; at more
tenants, overrides keyed by screen rather than by capability would remove the repetition. A
version outside `compat`, read from the product banner, would stop the run before it acts
(also not built: `compat` is `*`).

## 5. Escalation & handoff

**Detecting "stuck".** A human is brought in when the state library marks a screen
`escalate` (the member alert, which must not be auto-dismissed); when, after a commit, the
screen is unrecognised, a checkpoint isn't reached or an action times out; when an
irreversible action needs approval; and, during discovery, when the model calls
`request_help`. Before any commit, being stuck is a failure rather than a handoff: a clean,
retryable stop is cheaper than a person.

**Control model.** `cua.control` holds a lease on the live session, identified by an epoch.
Whoever acquires it invalidates every earlier token, and the session checks its token before
resolving a target and again right before acting, so the agent can never race an operator.
An intervention request carries the run, capability, step, the (redacted) reason and a
snapshot with a screenshot. There are three ways in: the engine escalates; an operator
**pauses** (the agent hands over at its next action, and the run continues after); or an
operator **takes over** (the lease moves, and the run ends as `aborted`).

**The handoff.** The run pauses on the same headed browser session it was using, not a fresh
one. The operator console, a small local web app, shows the request and screenshot; the
person acts directly in that browser window, and an injected script records what they did
(clicks by accessible name; fills by length only, since what a person types can be anything).
On *hand back*, the engine re-acquires the lease under a new epoch, reclassifies the screen
and continues; the result lists the intervention with operator, actions and resolution.
Unattended (the default), requests fail closed: approvals are rejected, handoffs abort.

Evidence: run `20260923T194339Z-f6c82a` and the two console screenshots, where member 23456's
alert was handed to me, acknowledged in the live window and the run finished with outputs.
Handing back without fixing the screen escalates again, and after two attempts the run fails
`recovery_exhausted`.

Mocked: the console has no authentication, self-declared identity and one local session.
Production needs an intervention queue with SLAs, SSO, and the live browser streamed to the
operator (CDP screencast or VNC).

## 6. Safety

**Hard limits, for every actor and mode:** an origin allowlist (per tenant, at runtime); a
path allowlist and denylist per app (`/signoff` and `/__admin` are outside); an action-kind
allowlist (CoreOne omits `press`: every flow works by click, fill and select, and a key press
can submit a form without a named button whose risk the policy could judge); only the runtime
may fill password fields; at most 200 actions and 1 irreversible action per run. Discovery
isn't offered tools for disallowed actions, and the session refuses them anyway.

**Risk is never taken on trust.** The policy infers it from the element acted on
(`confirm`, `submit`, `post`, `transfer`, `pay`, `delete`… are irreversible), the artifact
declares it, and the higher wins; mismatches are logged. At approval, a reviewer may only
lower reversible steps to read-only (the discovered search clicks). Read-only actions run;
reversible ones run in discovery and from approved capabilities; **irreversible actions need
a person's approval every time**, and are denied outright for unapproved capabilities. I
chose approval over blocking because banks need these actions done; at this maturity, none
should happen unattended. Discovery stops before commits by design: the sub-account flow ends
on the review screen and the policy refused *Confirm* when a scripted model tried it.

**Unattended use is measured, not granted.** Approval judges what a capability does; running
with nobody watching also depends on how reliably it does it. `cua stability` replays an
approved capability ten times unattended over two input sets and saves a report beside it:
a verdict (`stable`, `drifting`, `flaky`, `broken`), success rate, whether identical inputs
always agreed, drift, recoveries and durations. The summary is re-derived from the runs on
load, so a report can't claim more than its runs show. Unattended `replay` requires the
app's thresholds (`policy.json`: 10 runs, all successful, none drifting, within 30 days) and
otherwise exits 2 with the reason; `--operator console` runs on approval alone, since a
person can step in. Recoveries are reported but not scored: they are the app misbehaving and
the engine coping. Reports are keyed by the exact version, so a new base or overlay starts
uncleared; irreversible capabilities are never measured or cleared, since every run needs a
person and measuring would repeat commits. It is a run-level check made before the session
exists, not a policy rule, because the per-action policy doesn't know whether anyone is
watching.

All four capability × tenant combinations measured 10/10 with no drift, which says little
about a deterministic mock; the tests show the gate refusing flaky, broken, drifting, short
and stale reports. In production the score would come from a rolling window of real replays,
with clearance removed at the first drift or unexpected failure.

**Secrets and data.** Credentials are `env:` references resolved at the last moment and never
reach the model, artifacts or logs; password fields are masked in screenshots. Redaction is
value-based at every sink: declared-sensitive inputs, extracted outputs and secrets are
registered as the run goes and replaced by placeholders (`«inputs.member_id»`), with SSN and
card-number patterns as a backstop. Discovery transcripts keep the model's reasoning but not
the screens it saw.

**Found in real runs, and fixed:** the catalog stored the unredacted verification result next
to the artifact (now only kind and run ids); and a member's name reached an artifact through
a locator anchor (§3; now pruned, with a sensitive-value check on save).

**Limits.** Value-based redaction can't know data nobody declared: snapshots show whatever was
on screen, and discovery's `events.jsonl` and draft keep locators as synthesized, before
pruning (the log is append-only, so I don't rewrite it). The model sees screens, so discovery
belongs on a sandbox tenant with synthetic members and a provider with zero data retention.
Irreversibility inference is a name heuristic: an "OK" button that posts a transaction looks
reversible, which is what per-app `irreversible_patterns` and review are for. What a human
does during a handoff is recorded but not gated.

## 7. Cuts

Deliberately left out:

- **Scoped discovery for overlays** (§4): overlays are hand-written from drift evidence,
  then verified and approved; no LLM writes them, and there is no `compat` check.
- **Desktop and vision adapters**: the seam exists; no second adapter.
- **A production operator console**: no auth, one session, a local window rather than
  streaming.
- **Scale**: one live session per process, synchronous; no queue or workers.
- **Verification of irreversible flows**: verification must not commit, so a flow through
  *Confirm* can't be proven by replay. The discovered capability stops at review; opening the
  account is a hand-written fixture, exercised only in tests and `06`.
- **LLM limits**: a turn budget per discovery, but no cost or rate limits and no run-level
  deadline; one provider behind the `Planner` protocol.
- **On-screen data detection**: undeclared data in snapshots (§6).

Stretch goals, two as the brief suggests: cross-tenant reuse with per-variant overrides (§4),
and confidence & approval: the draft → approved gate plus a measured stability score that
clears approved capabilities for unattended replay (§6), as a batch rather than a rolling
window over real runs.

Next, in order: drift-triggered, scoped discovery that drafts overlays; exposing approved
capabilities as a typed tool catalog for agents; entity detection on screens before snapshots
and transcripts are written; sandbox-tenant verification for irreversible flows; stability
from a rolling window of production replays, with clearance revoked on drift; then
queue-backed workers with one session each.
