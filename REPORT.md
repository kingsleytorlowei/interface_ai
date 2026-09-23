# Design Report

## 1. Architecture

Two *drivers* — **discovery** (LLM-driven) and **replay** (deterministic) — sit on top of a single
guarded chokepoint, `GuardedSession`, through which every action flows. They never depend on each
other; the only thing they share is the artifact schema.

```
   goal+params → discovery (LLM loop + recorder)      replay (engine + classifier) ← artifact+params
                                  \                    /
                                   ▼                  ▼
                 GuardedSession: policy.check → lease.check → surface.act → evidence.emit
                                   |                  |
                                surface            control  ◄──►  operator (mock console)
```

| Module      | Responsibility                                                                 |
|-------------|---------------------------------------------------------------------------------|
| `schema`    | Pure types: capability artifact, `Target`, `Step`, `StateSignature`, `RunResult`. No I/O. |
| `surface`   | Perception/action port (`observe`, `resolve`, `act`) + web adapter.            |
| `policy`    | Allowlist, action risk classes, `Allow / Deny / RequireApproval`, redaction.   |
| `control`   | Control lease (who holds the session), intervention requests, resume.          |
| `session`   | `GuardedSession` — composes policy + lease + surface + evidence.               |
| `discovery` | Observe → decide → act loop; recorder turns actions into semantic steps.       |
| `replay`    | Deterministic step execution, state classification, recovery, result contract. |
| `apps`      | Per-app state library (known error/interstitial screens), tenant overrides.    |
| `evidence`  | Run directories, redacted JSONL event log, failure snapshots.                  |
| `store`     | Artifact persistence, versioning, approval state.                              |
| `operator`  | Mock operator console implementing the control port.                           |
| `cli`       | Composition root.                                                              |

Invariants:
1. Only `GuardedSession` acts on the surface — guardrails, control checks and evidence cannot be bypassed.
2. Artifacts reference normalized `Target`s, never adapter handles or coordinates.
3. The LLM exists only in `discovery`; replay cannot import it.
4. Redaction is enforced at the sinks (`evidence`, `store`); sensitive params are never serialized.
5. Outcome classification has a single owner (the replay classifier).

_TODO: key decisions and trade-offs (language, surface tech, model, single process)._

## 2. Artifact schema

_TODO_

## 3. Determinism & error handling

_TODO_

## 4. Heterogeneity & multi-tenant

_TODO_

## 5. Escalation & handoff

_TODO_

## 6. Safety

_TODO_

## 7. Cuts

_TODO_
