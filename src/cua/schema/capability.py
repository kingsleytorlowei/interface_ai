"""The capability artifact: a typed, versioned, reviewable contract for one recorded flow.

It is what a calling agent sees (inputs, outputs, business outcomes, risk) and what replay
executes (entry, named targets, steps with checkpoints, success condition). It references the
app's state library by name and never contains coordinates, adapter handles, secrets or
concrete parameter values.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import BuildVersion, DottedId, Model, Risk, Sensitivity, Slug, template_refs
from .states import AppModel, StateKind
from .targets import Target


class ParamType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    MONEY = "money"
    BOOLEAN = "boolean"
    DATE = "date"


class InputSpec(Model):
    type: ParamType
    description: str
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    pattern: str | None = None
    enum: list[str] | None = None

    @model_validator(mode="after")
    def _checks(self) -> "InputSpec":
        if self.sensitivity is Sensitivity.SECRET:
            raise ValueError("secrets are never capability inputs; use a runtime secret reference")
        if (self.pattern or self.enum) and self.type is not ParamType.STRING:
            raise ValueError("pattern/enum only apply to string inputs")
        return self


class OutputSpec(Model):
    type: ParamType
    description: str
    sensitivity: Sensitivity = Sensitivity.INTERNAL

    @model_validator(mode="after")
    def _no_secrets(self) -> "OutputSpec":
        if self.sensitivity is Sensitivity.SECRET:
            raise ValueError("secrets are never capability outputs")
        return self


class OutcomeSpec(Model):
    """A legitimate business result the caller must handle (not an error)."""

    description: str
    when_state: Slug


class Navigate(Model):
    kind: Literal["navigate"] = "navigate"
    url: str


class Click(Model):
    kind: Literal["click"] = "click"
    target: Slug


class Fill(Model):
    kind: Literal["fill"] = "fill"
    target: Slug
    value: str


class Select(Model):
    kind: Literal["select"] = "select"
    target: Slug
    option: str


class Press(Model):
    kind: Literal["press"] = "press"
    key: str
    target: Slug | None = None


class Extract(Model):
    """Read the target's text, parse it per the output's declared type, bind it to `into`."""

    kind: Literal["extract"] = "extract"
    target: Slug
    into: Slug


Action = Annotated[
    Navigate | Click | Fill | Select | Press | Extract, Field(discriminator="kind")
]


class Checkpoint(Model):
    state: Slug
    timeout_ms: int = Field(default=10_000, gt=0)


class Step(Model):
    id: Slug
    intent: str
    action: Action
    risk: Risk
    expect: Checkpoint | None = None


class AppRef(Model):
    app_id: Slug
    surface: Literal["web", "desktop"] = "web"
    compat: str = "*"


class Entry(Model):
    url: str
    requires_session: bool = True


class SuccessCondition(Model):
    state: Slug
    outputs_present: list[Slug] = []


class Status(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class Provenance(Model):
    run_id: str
    model: str
    recorded_at: datetime
    reviewed_by: str | None = None


def _action_templates(action: Action) -> list[str]:
    match action:
        case Navigate(url=url):
            return [url]
        case Fill(value=value):
            return [value]
        case Select(option=option):
            return [option]
        case _:
            return []


def _action_target(action: Action) -> str | None:
    return getattr(action, "target", None)


class Capability(Model):
    schema_version: Literal["1"] = "1"
    id: DottedId
    version: BuildVersion
    status: Status = Status.DRAFT
    description: str
    app: AppRef
    risk: Risk
    entry: Entry
    inputs: dict[Slug, InputSpec] = {}
    outputs: dict[Slug, OutputSpec] = {}
    outcomes: dict[Slug, OutcomeSpec] = {}
    targets: dict[Slug, Target] = {}
    steps: list[Step] = Field(min_length=1)
    success: SuccessCondition
    provenance: Provenance

    @model_validator(mode="after")
    def _consistent(self) -> "Capability":
        errors: list[str] = []

        if self.id.split(".")[0] != self.app.app_id:
            errors.append(f"id {self.id!r} must be namespaced by app_id {self.app.app_id!r}")

        step_ids = [s.id for s in self.steps]
        if dupes := {i for i in step_ids if step_ids.count(i) > 1}:
            errors.append(f"duplicate step ids: {sorted(dupes)}")

        used_targets = {t for s in self.steps if (t := _action_target(s.action))}
        if missing := used_targets - self.targets.keys():
            errors.append(f"steps reference undefined targets: {sorted(missing)}")
        if unused := self.targets.keys() - used_targets:
            errors.append(f"targets defined but never used: {sorted(unused)}")

        templates = [self.entry.url] + [t for s in self.steps for t in _action_templates(s.action)]
        referenced = set().union(*(template_refs(t, "inputs") for t in templates))
        if missing := referenced - self.inputs.keys():
            errors.append(f"templates reference undeclared inputs: {sorted(missing)}")
        if unused := self.inputs.keys() - referenced:
            errors.append(f"inputs declared but never used: {sorted(unused)}")

        extracted = [s.action.into for s in self.steps if isinstance(s.action, Extract)]
        if missing := set(extracted) - self.outputs.keys():
            errors.append(f"extract into undeclared outputs: {sorted(missing)}")
        if dupes := {o for o in extracted if extracted.count(o) > 1}:
            errors.append(f"outputs extracted more than once: {sorted(dupes)}")
        if never := self.outputs.keys() - set(extracted):
            errors.append(f"outputs never extracted: {sorted(never)}")
        if missing := set(self.success.outputs_present) - self.outputs.keys():
            errors.append(f"success condition references undeclared outputs: {sorted(missing)}")

        # An outcome ends the run the moment its screen shows, so it can't sit on the happy
        # path (success screen, or a screen a step passes through), and replay maps each
        # screen to exactly one outcome.
        on_path = {self.success.state} | {s.expect.state for s in self.steps if s.expect}
        for code, outcome in self.outcomes.items():
            if outcome.when_state in on_path:
                errors.append(f"outcome {code!r} maps to {outcome.when_state!r}, a screen on "
                              "the success path; outcomes are screens where the flow ends "
                              "instead of succeeding")
        states = [o.when_state for o in self.outcomes.values()]
        if dupes := {s for s in states if states.count(s) > 1}:
            errors.append(f"several outcomes map to the same screen: {sorted(dupes)}")

        max_risk = max((s.risk for s in self.steps), key=lambda r: r.rank)
        if self.risk is not max_risk:
            errors.append(f"capability risk {self.risk} must equal highest step risk {max_risk}")

        if self.status is Status.APPROVED and not self.provenance.reviewed_by:
            errors.append("approved capabilities must record provenance.reviewed_by")

        if errors:
            raise ValueError("; ".join(errors))
        return self

    def referenced_states(self) -> set[str]:
        states = {self.success.state} | {o.when_state for o in self.outcomes.values()}
        return states | {s.expect.state for s in self.steps if s.expect}


def check_against_app(capability: Capability, app: AppModel) -> list[str]:
    """Cross-artifact checks: state references must resolve in the app's state library."""
    errors: list[str] = []
    if capability.app.app_id != app.app_id:
        errors.append(f"capability targets app {capability.app.app_id!r}, not {app.app_id!r}")
    if missing := capability.referenced_states() - app.states.keys():
        errors.append(f"unknown states: {sorted(missing)}")
    for code, outcome in capability.outcomes.items():
        state = app.states.get(outcome.when_state)
        if state and state.kind is not StateKind.SCREEN:
            errors.append(f"outcome {code!r} must map to a screen state, not {state.kind}")
    return errors
