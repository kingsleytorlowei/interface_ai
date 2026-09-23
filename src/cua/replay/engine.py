"""Deterministic execution of one capability against a guarded session.

Every step is followed by classification of the resulting screen (`_settle`), which is the
single owner of "what happened": the expected state continues the run; a capability outcome
state ends it as a business outcome; interstitials are recovered per the app's state library;
fatal screens fail; anything else waits out the checkpoint and then fails — or, once a step
may have committed, escalates to a human, because at that point neither restarting nor
walking away is safe.
"""

import time
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from cua.apps import Classification, classify
from cua.schema import (
    Aborted,
    Action,
    BusinessOutcome,
    Capability,
    Checkpoint,
    Click,
    DriftSignal,
    Extract,
    Failure,
    FailureCategory,
    Fill,
    Navigate,
    RecoveryRecord,
    Risk,
    RunResult,
    Select,
    Sensitivity,
    StateKind,
    StateSignature,
    Step,
    Success,
    check_against_app,
    render_template,
)
from cua.schema.common import template_refs
from cua.schema.results import OutputValue
from cua.schema.states import ClickRecovery, EscalateRecovery, ReauthenticateRecovery, RetryRecovery
from cua.session import (
    ActionFailed,
    ApprovalRejected,
    Command,
    GuardedSession,
    InterventionAborted,
    LeaseLost,
    PolicyDenied,
    SessionError,
    SignOnFailed,
    TargetError,
)

from .inputs import InputError, InputValue, coerce_inputs, template_value
from .outputs import OutputError, parse_output

# Worth retrying the whole capability later, provided nothing has committed.
TRANSIENT = {FailureCategory.TIMEOUT, FailureCategory.APP_ERROR,
             FailureCategory.RECOVERY_EXHAUSTED}


class _Stop(Exception):
    """Ends the run with a terminal result."""

    def __init__(self, kind: type[RunResult], **fields: Any) -> None:  # type: ignore[valid-type]
        self.kind = kind
        self.fields = fields


class _Restart(Exception):
    """Start again from the entry point. Only raised while nothing has committed."""


def replay(
    capability: Capability, params: Mapping[str, Any], session: GuardedSession, **options: Any
) -> RunResult:
    return Replayer(capability, session, **options).run(params)


class Replayer:
    def __init__(
        self,
        capability: Capability,
        session: GuardedSession,
        *,
        poll_interval_s: float = 0.25,
        settle_timeout_ms: int = 5000,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if errors := check_against_app(capability, session.app):
            raise ValueError(f"{capability.id} does not fit app {session.app.app_id}: {errors}")
        self.cap = capability
        self.session = session
        self.app = session.app
        self.log = session.log
        self._poll_s = poll_interval_s
        self._settle_timeout_ms = settle_timeout_ms
        self._sleep = sleep
        self._outcome_codes = {o.when_state: code for code, o in capability.outcomes.items()}

        self._started_at = datetime.now(UTC)
        self._inputs: dict[str, InputValue] = {}
        self._outputs: dict[str, OutputValue] = {}
        self._recoveries: list[RecoveryRecord] = []
        self._recovery_counts: Counter[str] = Counter()
        self._interventions_before = len(session.interventions)
        self._drift: list[DriftSignal] = []
        self._committed: list[str] = []
        self._step: Step | None = None

    # run --------------------------------------------------------------------------------

    def run(self, params: Mapping[str, Any]) -> RunResult:
        self._started_at = datetime.now(UTC)
        self.log.emit("run_started", capability=self.cap.id, version=self.cap.version,
                      status=self.cap.status)
        try:
            self._accept_inputs(params)
            result = self._execute()
        except _Stop as stop:
            result = self._result(stop.kind, **stop.fields)
        except SessionError as e:
            result = self._from_session_error(e)
        self.log.write_json("result.json", result.model_dump(mode="json"))
        self.log.emit("run_finished", kind=result.kind,
                      category=getattr(result, "category", None),
                      code=getattr(result, "code", None), committed=self._committed)
        return result

    def _accept_inputs(self, params: Mapping[str, Any]) -> None:
        # Register raw sensitive values first, so even a rejection message can't leak them.
        for name, spec in self.cap.inputs.items():
            if spec.sensitivity.masked_in_logs and isinstance(params.get(name), str):
                self.session.mark_sensitive(params[name], f"inputs.{name}")
        try:
            self._inputs = coerce_inputs(self.cap.inputs, params)
        except InputError as e:
            raise self._failure(FailureCategory.INVALID_INPUT, "inputs matching the input specs",
                                str(e), f"invalid inputs: {e}") from e
        for name, value in self._inputs.items():
            if self.cap.inputs[name].sensitivity.masked_in_logs:
                self.session.mark_sensitive(template_value(value), f"inputs.{name}")
        self.log.emit("inputs_accepted", inputs={k: template_value(v)
                                                 for k, v in self._inputs.items()})

    def _execute(self) -> RunResult:
        if self.cap.entry.requires_session and not self.session.signed_on:
            self.session.sign_on()
        while True:
            try:
                return self._attempt()
            except _Restart:
                self.log.emit("run_restarted")

    def _attempt(self) -> RunResult:
        self._outputs = {}
        self._step = None
        self.session.execute(Command(Navigate(url=self._render(self.cap.entry.url)),
                                     step_id="entry"))
        self._settle(None)
        for step in self.cap.steps:
            self._step = step
            self.log.emit("step_started", step.id, intent=step.intent)
            self._run_step(step)
            self._settle(step.expect)
        self._step = None
        self._settle(Checkpoint(state=self.cap.success.state))
        if missing := [o for o in self.cap.success.outputs_present if o not in self._outputs]:
            raise self._failure(FailureCategory.OUTPUT_INVALID, f"outputs {missing}",
                                "not extracted", f"success without outputs {missing}")
        return self._result(Success, outputs=dict(self._outputs))

    def _run_step(self, step: Step) -> None:
        action = self._render_action(step.action)
        slug = getattr(action, "target", None)
        cmd = Command(
            action=action,
            target=self.cap.targets[slug] if slug else None,
            declared_risk=step.risk,
            step_id=step.id,
            sensitivity=self._sensitivity(step.action),
        )
        try:
            report = self.session.execute(cmd)
        except ActionFailed as e:
            if self._commits(step.risk, e.risk):
                self._committed.append(step.id)
            raise
        if self._commits(step.risk, report.effective_risk):
            self._committed.append(step.id)
        if report.strategy_index and slug:
            self._drift.append(DriftSignal(step_id=step.id, target=slug,
                                           strategy_index=report.strategy_index))
        if isinstance(action, Extract):
            spec = self.cap.outputs[action.into]
            try:
                self._outputs[action.into] = parse_output(report.text or "", spec)
            except OutputError as e:
                self.session.capture("output-invalid", step.id)
                raise self._failure(FailureCategory.OUTPUT_INVALID,
                                    f"{action.into} as {spec.type}", str(e),
                                    f"could not read {action.into}: {e}") from e
            if spec.sensitivity.masked_in_logs:
                # The parsed form ("4210.37") differs from what was on screen ("$4,210.37").
                self.session.mark_sensitive(str(self._outputs[action.into]), action.into)

    @staticmethod
    def _commits(declared: Risk, effective: Risk) -> bool:
        """May this step have changed something that a restart would repeat? The reviewed
        artifact vouches for read-only steps (the UI can't tell a search submit from a
        mutation), but a commit the policy detects on screen overrides that claim."""
        return declared is not Risk.READ_ONLY or effective is Risk.IRREVERSIBLE

    # classification ---------------------------------------------------------------------

    def _settle(self, expect: Checkpoint | None) -> None:
        timeout_s = (expect.timeout_ms if expect else self._settle_timeout_ms) / 1000
        deadline = time.monotonic() + timeout_s
        escalated = False
        while True:
            c = classify(self.app, self.session.observe(quiet=True))
            if expect and c.state == expect.state:
                return
            if c.state in self._outcome_codes:
                code = self._outcome_codes[c.state]
                raise _Stop(BusinessOutcome, code=code,
                            message=self.cap.outcomes[code].description)
            sig = self.app.states.get(c.state) if c.state else None
            if sig and sig.kind is StateKind.FATAL:
                self.session.capture("fatal", self._step_id)
                raise self._failure(FailureCategory.APP_ERROR, self._expected(expect),
                                    f"{c.state}: {sig.description}",
                                    f"application error screen ({c.state})")
            if sig and sig.kind is StateKind.INTERSTITIAL:
                assert c.state
                self._recover(c.state, sig)
                deadline = time.monotonic() + timeout_s
                continue
            if expect is None and not c.ambiguous:
                return  # a plain (or unrecognised) screen; the next step's target decides
            if time.monotonic() >= deadline:
                if self._committed and not escalated:
                    escalated = True
                    self._escalate(f"expected {self._expected(expect)}, observed "
                                   f"{_observed(c)}; steps {self._committed} may have "
                                   "taken effect")
                    deadline = time.monotonic() + timeout_s
                    continue
                self.session.capture("checkpoint-mismatch", self._step_id)
                category = (FailureCategory.CHECKPOINT_MISMATCH if c.state
                            else FailureCategory.UNKNOWN_STATE)
                raise self._failure(category, self._expected(expect), _observed(c),
                                    f"expected {self._expected(expect)}, "
                                    f"observed {_observed(c)}")
            self._sleep(self._poll_s)

    def _recover(self, state: str, sig: StateSignature) -> None:
        self._recovery_counts[state] += 1
        attempt = self._recovery_counts[state]
        if attempt > sig.max_recoveries:
            self.session.capture("recovery-exhausted", self._step_id)
            raise self._failure(FailureCategory.RECOVERY_EXHAUSTED,
                                f"{state} to clear within {sig.max_recoveries} recoveries",
                                state, f"{state} persisted after {sig.max_recoveries} recoveries")
        assert sig.recovery is not None
        self._recoveries.append(RecoveryRecord(step_id=self._step_id, state=state,
                                               recovery=sig.recovery.kind, attempt=attempt))
        self.log.emit("recovery", self._step_id, state=state, recovery=sig.recovery.kind,
                      attempt=attempt)
        match sig.recovery:
            case ClickRecovery(target=target):
                self.session.execute(Command(Click(target=f"{state}_dismiss"), target,
                                             step_id=self._step_id))
            case RetryRecovery(wait_ms=wait_ms):
                if self._restart_is_unsafe(state):
                    return
                self._sleep(wait_ms / 1000)
                raise _Restart()
            case ReauthenticateRecovery():
                if self._restart_is_unsafe(state):
                    return
                self.session.sign_on()
                raise _Restart()
            case EscalateRecovery():
                self._escalate(sig.description or state)

    def _restart_is_unsafe(self, state: str) -> bool:
        """Restarting would repeat committed steps: hand over to a human instead."""
        if not self._committed:
            return False
        self._escalate(f"{state}: restarting would repeat steps {self._committed}, which may "
                       "have taken effect")
        return True

    def _escalate(self, reason: str) -> None:
        intervention = self.session.request_intervention(reason, self._step_id)
        if intervention.resolution == "aborted":
            raise _Stop(Aborted, step_id=self._step_id, reason=f"operator aborted: {reason}")

    # results ----------------------------------------------------------------------------

    def _from_session_error(self, e: SessionError) -> RunResult:
        match e:
            case ApprovalRejected():
                return self._result(Aborted, step_id=self._step_id, reason=str(e))
            case LeaseLost():
                return self._result(Aborted, step_id=self._step_id,
                                    reason="an operator took control of the session")
            case InterventionAborted():
                return self._result(Aborted, step_id=self._step_id, reason=str(e))
            case PolicyDenied():
                expected = f"the action to be permitted (rule {e.rule})"
            case TargetError():
                slug = getattr(self._step.action, "target", None) if self._step else None
                expected = f"target {slug or '?'} to resolve to exactly one element"
            case SignOnFailed():
                expected = "sign-on to succeed"
            case _:
                expected = "the action to complete"
        stop = self._failure(e.category or FailureCategory.APP_ERROR, expected, str(e), str(e))
        return self._result(stop.kind, **stop.fields)

    def _failure(self, category: FailureCategory, expected: str, observed: str,
                 message: str) -> _Stop:
        return _Stop(Failure, category=category, step_id=self._step_id, expected=expected,
                     observed=observed, message=message,
                     retryable=not self._committed and category in TRANSIENT)

    def _result(self, kind: type[RunResult], **fields: Any) -> RunResult:  # type: ignore[valid-type]
        return kind(  # type: ignore[no-any-return]
            run_id=self.session.run_id,
            capability_id=self.cap.id,
            capability_version=self.cap.version,
            started_at=self._started_at,
            finished_at=datetime.now(UTC),
            recoveries=list(self._recoveries),
            # every handoff in this run, including pauses an operator asked for
            interventions=self.session.interventions[self._interventions_before:],
            drift=list(self._drift),
            committed_steps=list(self._committed),
            evidence_ref=str(self.log.dir),
            **fields,
        )

    # helpers ----------------------------------------------------------------------------

    @property
    def _step_id(self) -> str | None:
        return self._step.id if self._step else None

    @staticmethod
    def _expected(expect: Checkpoint | None) -> str:
        return expect.state if expect else "a recognisable screen"

    def _render(self, text: str) -> str:
        inputs = {k: template_value(v) for k, v in self._inputs.items()}
        return render_template(text, inputs=inputs, env=self.session.env)

    def _render_action(self, action: Action) -> Action:
        match action:
            case Navigate(url=url):
                return action.model_copy(update={"url": self._render(url)})
            case Fill(value=value):
                return action.model_copy(update={"value": self._render(value)})
            case Select(option=option):
                return action.model_copy(update={"option": self._render(option)})
        return action

    def _sensitivity(self, action: Action) -> Sensitivity:
        """Of the value this step types or reads, so the session can redact it."""
        match action:
            case Extract(into=into):
                return self.cap.outputs[into].sensitivity
            case Fill(value=template) | Select(option=template):
                for name in sorted(template_refs(template, "inputs")):
                    if (s := self.cap.inputs[name].sensitivity).masked_in_logs:
                        return s
        return Sensitivity.INTERNAL


def _observed(c: Classification) -> str:
    if c.state:
        return c.state
    return f"ambiguous: {', '.join(c.matched)}" if c.ambiguous else "no known state"
