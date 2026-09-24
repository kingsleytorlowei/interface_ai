"""Turns executed actions into a draft capability. Deterministic: no LLM here.

Every successful action is recorded as a candidate step with the target the session actually
used (already verified to resolve once), the risk the policy actually applied, and a
checkpoint taken from classifying the screen it led to. The model only chooses which
candidates form the flow (`finish.steps`); the verification replay then has to prove that
flow works on its own.
"""

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TypeGuard

from cua.schema import (
    Action,
    AppModel,
    AppRef,
    Capability,
    Checkpoint,
    Entry,
    Extract,
    Fill,
    InputSpec,
    OutcomeSpec,
    Provenance,
    Risk,
    Select,
    StateKind,
    Step,
    SuccessCondition,
    Target,
    check_against_app,
)
from cua.schema.common import template_refs

from .goal import Goal


class RecordingError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Candidate:
    step_id: str
    intent: str
    action: Action
    target_name: str | None
    risk: Risk
    after_state: str | None


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s if s and s[0].isalpha() else f"t_{s}" if s else "element"


class Recorder:
    def __init__(self, goal: Goal, app: AppModel) -> None:
        self.goal = goal
        self.app = app
        self.candidates: dict[str, Candidate] = {}
        self.targets: dict[str, Target] = {}
        self.rewrites: list[str] = []  # literal example values turned back into templates

    def parameterize(self, text: str) -> str:
        """Backstop: a literal that is exactly an example input value becomes its template,
        so example data (often PII) never gets baked into an artifact."""
        for name, example in self.goal.examples().items():
            if text == example:
                self.rewrites.append(name)
                return f"{{{{inputs.{name}}}}}"
        return text

    def name_target(self, proposed: str, target: Target) -> str:
        """The model's name for an element, made unique per distinct target."""
        base = slug(proposed)
        name, n = base, 1
        while name in self.targets and self.targets[name] != target:
            n += 1
            name = f"{base}_{n}"
        self.targets[name] = target
        return name

    def record(self, *, verb: str, intent: str, action: Action, target_name: str | None,
               risk: Risk, after_state: str | None) -> str:
        base = f"{verb}_{target_name}" if target_name else verb
        step_id, n = base, 1
        while step_id in self.candidates:
            n += 1
            step_id = f"{base}_{n}"
        self.candidates[step_id] = Candidate(step_id, intent, action, target_name, risk,
                                             after_state)
        return step_id

    def settle_last(self, state: str | None) -> None:
        """The last step led to an interstitial that has since been cleared (by the model, or
        by a human): its checkpoint is the screen reached after it, not the interstitial."""
        if self.candidates and self._is_screen(state):
            last = next(reversed(self.candidates))
            self.candidates[last] = replace(self.candidates[last], after_state=state)

    def build(self, step_ids: list[str], outcomes: dict[str, OutcomeSpec], *, run_id: str,
              model: str) -> Capability:
        problems: list[str] = []
        if unknown := [s for s in step_ids if s not in self.candidates]:
            problems.append(f"unknown step ids {unknown}; recorded: {list(self.candidates)}")
        if dupes := {s for s in step_ids if step_ids.count(s) > 1}:
            problems.append(f"steps listed more than once: {sorted(dupes)}")
        chosen = [self.candidates[s] for s in step_ids if s in self.candidates]
        if not chosen:
            problems.append("no steps chosen")
        if problems:
            raise RecordingError(problems)

        steps = [
            Step(id=c.step_id, intent=c.intent, action=c.action, risk=c.risk,
                 expect=Checkpoint(state=c.after_state) if self._is_screen(c.after_state)
                 else None)
            for c in chosen
        ]
        final = chosen[-1].after_state
        if not self._is_screen(final):
            problems.append(f"the flow ends on {final or 'an unrecognised screen'}, "
                            "not a known screen state")

        used_inputs = set().union(*(template_refs(t, "inputs") for t in
                                    [self.goal.entry, *(_templates(c.action) for c in chosen)]))
        if unused := set(self.goal.inputs) - used_inputs:
            problems.append(f"goal inputs never used: {sorted(unused)} "
                            "(type them as {{inputs.<name>}})")
        extracted = {c.action.into for c in chosen if isinstance(c.action, Extract)}
        if missing := set(self.goal.outputs) - extracted:
            problems.append(f"goal outputs never extracted: {sorted(missing)}")
        if problems:
            raise RecordingError(problems)

        used_targets = {c.target_name for c in chosen if c.target_name}
        try:
            capability = self._capability(steps, used_inputs, used_targets, final, outcomes,
                                          run_id=run_id, model=model)
        except ValueError as e:  # the schema's own consistency checks
            raise RecordingError([str(e)]) from e
        if errors := check_against_app(capability, self.app):
            raise RecordingError(errors)
        return capability

    def _capability(self, steps: list[Step], used_inputs: set[str], used_targets: set[str],
                    final: str | None, outcomes: dict[str, OutcomeSpec], *, run_id: str,
                    model: str) -> Capability:
        return Capability(
            id=self.goal.capability_id,
            version="0.1.0",
            description=self.goal.title or self.goal.goal,
            app=AppRef(app_id=self.app.app_id),
            risk=max((s.risk for s in steps), key=lambda r: r.rank),
            entry=Entry(url=self.goal.entry, requires_session=self.goal.requires_session),
            inputs={n: InputSpec(**s.model_dump(exclude={"example", "alt_example"}))
                    for n, s in self.goal.inputs.items() if n in used_inputs},
            outputs=dict(self.goal.outputs),
            outcomes=outcomes,
            targets={n: t for n, t in self.targets.items() if n in used_targets},
            steps=steps,
            success=SuccessCondition(state=final or "",
                                     outputs_present=sorted(self.goal.outputs)),
            provenance=Provenance(run_id=run_id, model=model, recorded_at=datetime.now(UTC)),
        )

    def _is_screen(self, state: str | None) -> TypeGuard[str]:
        return state is not None and self.app.states[state].kind is StateKind.SCREEN


def _templates(action: Action) -> str:
    match action:
        case Fill(value=value):
            return value
        case Select(option=option):
            return option
    return ""


def prune_strategies(capability: Capability, audits: Sequence[Mapping[str, list[bool]]],
                     only: Collection[str] | None = None) -> tuple[Capability, list[str]]:
    """Keep only the strategies that identified their element in every verification run
    (with `only`, just for those targets: a tenant overlay prunes its own, never the base's).

    A strategy synthesized from one screen may lean on that screen's data (an anchor like
    "OPEN SUB-ACCOUNT — <member name>"): it is useless for any other input, and it carries
    the data into the artifact. Replaying with different inputs and dropping whatever didn't
    hold removes both. Notes name strategies by position and kind only, never their text.
    """
    targets: dict[str, Target] = {}
    notes: list[str] = []
    for name, target in capability.targets.items():
        if only is not None and name not in only:
            targets[name] = target
            continue
        runs = [audit[name] for audit in audits if name in audit]
        if not runs:
            notes.append(f"target {name} was never resolved during verification; its "
                         f"{len(target.strategies)} strategies are unproven")
            targets[name] = target
            continue
        keep = [all(run[i] for run in runs) for i in range(len(target.strategies))]
        if not any(keep):
            raise RecordingError([f"no strategy of target {name} identified its element in "
                                  "every verification run"])
        for i, (strategy, kept) in enumerate(zip(target.strategies, keep, strict=True)):
            if not kept:
                notes.append(f"target {name}: dropped strategy #{i + 1} ({strategy.by}), "
                             "which did not identify the element in every verification run")
        targets[name] = target.model_copy(update={
            "strategies": [s for s, k in zip(target.strategies, keep, strict=True) if k]})
    return capability.model_copy(update={"targets": targets}), notes
