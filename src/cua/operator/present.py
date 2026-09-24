"""Capabilities and results as a bank employee reads them: steps in their own words, risk as
what it means for the member's data, and a run's result as what happened and what to do next.
No JSON, locators or failure-category codes reach the page."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from cua.schema import (
    Aborted,
    BusinessOutcome,
    Capability,
    Failure,
    FailureCategory,
    ParamType,
    Risk,
    RunResult,
    Step,
    Success,
    describe,
)

RISK_WORDS = {
    Risk.READ_ONLY: "reads only",
    Risk.REVERSIBLE: "may change data",
    Risk.IRREVERSIBLE: "irreversible: needs approval every run",
}

RECOVERY_WORDS = {
    "click": "dismissed a notice",
    "retry": "retried after the application was unavailable or slow",
    "reauthenticate": "signed in again after the session expired",
    "escalate": "a person stepped in",
}


@dataclass(frozen=True)
class StepView:
    id: str
    number: int
    intent: str
    where: str  # the element, in words; "" for steps without one
    risk: Risk
    risk_words: str
    lowerable: bool  # a reviewer may mark it read-only (reversible steps only)


def steps(capability: Capability) -> list[StepView]:
    views = []
    for number, step in enumerate(capability.steps, 1):
        target = getattr(step.action, "target", None)
        views.append(StepView(
            id=step.id, number=number, intent=step.intent,
            where=describe(capability.targets[target]) if target else "",
            risk=step.risk, risk_words=RISK_WORDS[step.risk],
            lowerable=step.risk is Risk.REVERSIBLE))
    return views


def _screen(state: str) -> str:
    return state.replace("_", " ")


def ending(capability: Capability) -> str:
    return f"the {_screen(capability.success.state)} screen"


def outcomes(capability: Capability) -> list[str]:
    return [o.description for o in capability.outcomes.values()]


def format_value(value: object, kind: ParamType) -> str:
    if kind is ParamType.MONEY and isinstance(value, Decimal | int | float | str):
        return f"${Decimal(str(value)):,.2f}"
    return str(value)


@dataclass
class ResultView:
    tone: Literal["success", "outcome", "failure", "stopped"]
    headline: str
    details: list[str] = field(default_factory=list)
    outputs: list[tuple[str, str]] = field(default_factory=list)  # (label, value)
    advice: str = ""


def _step(capability: Capability, step_id: str | None) -> Step | None:
    return next((s for s in capability.steps if s.id == step_id), None)


def _at(capability: Capability, step_id: str | None) -> str:
    step = _step(capability, step_id)
    if step is not None:
        return f'at "{step.intent}"'
    return "while signing on" if step_id == "sign_on" else "before the first step"


def _failure_headline(capability: Capability, r: Failure) -> str:
    at = _at(capability, r.step_id)
    step = _step(capability, r.step_id)
    target = getattr(step.action, "target", None) if step else None
    where = describe(capability.targets[target]) if target else "the element"
    match r.category:
        case FailureCategory.INVALID_INPUT:
            return f"The inputs weren't accepted: {r.message}"
        case FailureCategory.POLICY_DENIED:
            return f"Stopped by a safety rule {at}: {r.message}"
        case FailureCategory.TARGET_NOT_FOUND:
            return f"Couldn't find the {where} {at}; the screen may have changed"
        case FailureCategory.TARGET_AMBIGUOUS:
            return f"Found more than one {where} {at}"
        case FailureCategory.CHECKPOINT_MISMATCH:
            return (f"Expected the {_screen(r.expected)} screen {at}, "
                    f"but the application showed {_screen(r.observed)}")
        case FailureCategory.UNKNOWN_STATE:
            return f"Reached a screen it doesn't recognise {at}"
        case FailureCategory.RECOVERY_EXHAUSTED:
            return f"Kept running into {_screen(r.observed)} {at} and gave up"
        case FailureCategory.TIMEOUT:
            return f"The application didn't respond in time {at}"
        case FailureCategory.APP_ERROR:
            return f"The application reported an error {at}"
        case FailureCategory.OUTPUT_INVALID:
            return f"Read a value {at} that isn't what was expected: {r.message}"
    return f"Failed {at}: {r.message}"


def result(capability: Capability, r: RunResult) -> ResultView:
    match r:
        case Success(outputs=outs):
            view = ResultView("success", "Done")
            view.outputs = [(capability.outputs[name].description,
                             format_value(value, capability.outputs[name].type))
                            for name, value in outs.items() if name in capability.outputs]
        case BusinessOutcome(code=code, message=message):
            spec = capability.outcomes.get(code)
            view = ResultView("outcome", spec.description if spec else message)
        case Failure():
            view = ResultView("failure", _failure_headline(capability, r))
        case Aborted(reason=reason, step_id=step_id):
            view = ResultView("stopped", f"Stopped {_at(capability, step_id)}: {reason}")
    if r.recoveries:
        handled = sorted({RECOVERY_WORDS.get(x.recovery, x.recovery) for x in r.recoveries})
        view.details.append("Handled on the way: " + "; ".join(handled) + ".")
    if r.interventions:
        people = sorted({i.operator or "an operator" for i in r.interventions})
        view.details.append("A person stepped in: " + ", ".join(people) + ".")
    if r.drift:
        view.details.append("Some elements were found through their fallbacks; the screen "
                            "may be changing.")
    if r.committed_steps:
        done = [(s.intent if (s := _step(capability, sid)) else sid) for sid in r.committed_steps]
        effect = "Took effect" if view.tone == "success" else "May already have taken effect"
        view.advice = f"{effect}: {', '.join(done)}."
        if view.tone != "success":
            view.advice += " Check before running it again."
    elif view.tone in ("failure", "stopped"):
        retryable = isinstance(r, Failure) and r.retryable
        view.advice = ("Nothing was changed; safe to try again." if retryable
                       else "Nothing was changed.")
    return view
