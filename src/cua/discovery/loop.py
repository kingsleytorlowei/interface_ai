"""The observe -> decide -> act loop. The model decides; every action goes through the guarded
session (policy, approvals, lease, evidence); the recorder writes down what actually worked.
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

from cua.apps import classify
from cua.evidence import snapshot_digest
from cua.schema import (
    Action,
    AppModel,
    Capability,
    Click,
    Extract,
    Fill,
    Navigate,
    Observation,
    OutcomeSpec,
    Press,
    Select,
    Sensitivity,
    StateKind,
    render_template,
)
from cua.schema.common import template_refs
from cua.schema.states import EscalateRecovery
from cua.session import (
    ActionFailed,
    ApprovalRejected,
    Command,
    GuardedSession,
    InterventionAborted,
    LeaseLost,
    PolicyDenied,
    Ref,
    TargetError,
)

from .goal import Goal
from .planner import Planner, ToolCall, ToolResult, Turn
from .recorder import Recorder, RecordingError, slug
from .tools import tool_definitions

Status = Literal["recorded", "gave_up", "aborted", "budget_exhausted", "refused"]
ACTIONS = ("click", "fill", "select", "press", "extract")


@dataclass
class DiscoveryResult:
    status: Status
    reason: str
    capability: Capability | None = None
    notes: str = ""
    review_notes: list[str] = field(default_factory=list)
    turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class _Outcome:
    content: str = ""
    is_error: bool = False
    terminal: DiscoveryResult | None = None


SYSTEM = """\
You are discovering how to perform a task in a business application, so that it can be \
recorded as a reusable, parameterised flow and replayed later without you.

How this works:
- You act through tools, one action per turn. Each result shows the new screen as an \
accessibility snapshot. Refer to elements by their ref from the latest snapshot only.
- Every successful action is recorded as a candidate step with an id. When done, call finish \
with the ids that make up the clean flow, in order, leaving out detours and mistakes.
- Give elements stable, descriptive target_name values; they become part of the recorded flow.
- Wherever the task uses an input, type its placeholder (for example {{inputs.member_id}}), \
never its example value. Placeholders are filled in when the flow runs.
- Read every requested output with extract.
- You are already signed in. Never enter credentials.
- Some actions need a human's approval or are refused by policy; the result says so. Do not \
try to work around a refusal.
- Screens marked as interstitials are handled automatically at replay: deal with them to \
continue (those actions are not recorded). If a screen says a person must act, call \
request_help.
- In finish, map the business results a caller must handle (such as no matching record, or \
access denied) to the screen states below, including ones you did not encounter this run.
- If you cannot complete the goal, call give_up with the reason.

Known screens of this application:
{states}
"""


def system_prompt(app: AppModel) -> str:
    lines = []
    for name, sig in sorted(app.states.items()):
        kind = "" if sig.kind is StateKind.SCREEN else f" [{sig.kind}]"
        lines.append(f"- {name}{kind}: {sig.description}")
    return SYSTEM.replace("{states}", "\n".join(lines))  # not .format: the prompt has {{ }}


def discover(goal: Goal, session: GuardedSession, planner: Planner) -> DiscoveryResult:
    return DiscoveryLoop(goal, session, planner).run()


class DiscoveryLoop:
    def __init__(self, goal: Goal, session: GuardedSession, planner: Planner) -> None:
        if goal.app != session.app.app_id:
            raise ValueError(f"goal is for app {goal.app!r}, session is {session.app.app_id!r}")
        self.goal = goal
        self.session = session
        self.app = session.app
        self.planner = planner
        self.recorder = Recorder(goal, self.app)
        self.state: str | None = None
        self.usage: Counter[str] = Counter()
        self.turns = 0
        self._screens_sent: dict[str, str] = {}  # snapshot text -> its digest in the events

    def run(self) -> DiscoveryResult:
        log = self.session.log
        log.emit("discovery_started", goal=self.goal.goal, capability=self.goal.capability_id,
                 model=self.planner.model)
        for name, spec in self.goal.inputs.items():
            if spec.sensitivity.masked_in_logs:
                self.session.mark_sensitive(spec.example, f"inputs.{name}")
        if self.goal.requires_session and not self.session.signed_on:
            self.session.sign_on()
        entry = render_template(self.goal.entry, env=self.session.env)
        self.session.execute(Command(Navigate(url=entry), step_id="entry"))
        obs = self.session.observe()
        self.state = classify(self.app, obs).state

        result = self._loop(self.planner.begin(system_prompt(self.app),
                                               tool_definitions(self.goal, self.app,
                                                                self.session.allowed_actions),
                                               self._task(obs)))
        result.turns, result.usage = self.turns, dict(self.usage)
        result.review_notes = self._review_notes(result.capability)
        log.write_json("transcript.json", self._without_screens(self.planner.transcript()))
        log.emit("discovery_finished", status=result.status, reason=result.reason,
                 turns=result.turns, usage=result.usage)
        return result

    def _loop(self, turn: Turn) -> DiscoveryResult:
        nudged = False
        while True:
            self.turns += 1
            self._account(turn)
            if turn.stop_reason == "refusal":
                return DiscoveryResult("refused", "the model declined the request")
            if not turn.calls:
                if nudged:
                    return DiscoveryResult("gave_up", "the model stopped without finishing")
                nudged = True
                turn = self.planner.respond([], note="Continue with a tool call, or call "
                                                     "finish or give_up.")
                continue
            if self.turns > self.goal.max_turns:
                return DiscoveryResult("budget_exhausted",
                                       f"no result within {self.goal.max_turns} turns")
            first, *extra = turn.calls
            outcome = self._dispatch(first)
            if outcome.terminal:
                return outcome.terminal
            results = [ToolResult(first.id, outcome.content, outcome.is_error)]
            results += [ToolResult(c.id, "Ignored: one action per turn.", True) for c in extra]
            turn = self.planner.respond(results)

    def _account(self, turn: Turn) -> None:
        self.usage.update(turn.usage)
        self.session.log.emit("llm_turn", turn=self.turns, stop_reason=turn.stop_reason,
                              usage=turn.usage, text=turn.text,
                              calls=[{"name": c.name, "input": c.input} for c in turn.calls])

    # tools ------------------------------------------------------------------------------

    def _dispatch(self, call: ToolCall) -> _Outcome:
        args = call.input
        match call.name:
            case name if name in ACTIONS:
                return self._act(name, args)
            case "request_help":
                return self._request_help(args["reason"])
            case "finish":
                return self._finish(args)
            case "give_up":
                return _Outcome(terminal=DiscoveryResult("gave_up", args["reason"]))
        return _Outcome(f"Unknown tool {call.name!r}.", is_error=True)

    def _act(self, verb: str, args: dict[str, Any]) -> _Outcome:
        before = self.app.states.get(self.state) if self.state else None
        if before and isinstance(before.recovery, EscalateRecovery):
            return _Outcome(f"Blocked: {self.state} needs a person to act. Call request_help.",
                            is_error=True)
        on_interstitial = before is not None and before.kind is StateKind.INTERSTITIAL

        try:
            action, rendered, sensitivity = self._action(verb, args)
        except KeyError as e:
            return _Outcome(f"Not done: {e.args[0]}", is_error=True)
        cmd = Command(rendered, Ref(args["ref"]), step_id=f"discovery_{verb}",
                      sensitivity=sensitivity)
        try:
            report = self.session.execute(cmd)
        except LeaseLost:
            return _Outcome(terminal=DiscoveryResult("aborted", "an operator took control"))
        except InterventionAborted as e:
            return _Outcome(terminal=DiscoveryResult("aborted", str(e)))
        except (PolicyDenied, ApprovalRejected, TargetError, ActionFailed) as e:
            obs = self.session.observe()
            self.state = classify(self.app, obs).state
            return _Outcome(f"Not done: {e}\n\n{self._screen(obs)}", is_error=True)

        after = classify(self.app, report.after).state
        if on_interstitial:
            self.recorder.settle_last(after)
            note = (f"Done (not recorded: {self.state} is an interstitial that replay handles "
                    "on its own).")
        else:
            assert report.target is not None
            name = self.recorder.name_target(args.get("target_name") or args["output_name"],
                                             report.target)
            step_id = self.recorder.record(
                verb=verb, intent=args["intent"], action=action.model_copy(update={"target": name}),
                target_name=name, risk=report.effective_risk, after_state=after)
            note = f"Done. Recorded as step `{step_id}` (risk: {report.effective_risk})."
        if report.text is not None:
            note += f" Read: {report.text!r}."
        self.state = after
        return _Outcome(f"{note}\n\n{self._screen(report.after)}")

    def _action(self, verb: str, args: dict[str, Any]) -> tuple[Action, Action, Sensitivity]:
        """(action to record, with templates; action to execute, rendered; value sensitivity)"""
        provisional = slug(args.get("target_name") or args.get("output_name") or verb)
        examples = self.goal.examples()
        match verb:
            case "click":
                action: Action = Click(target=provisional)
                return action, action, Sensitivity.INTERNAL
            case "press":
                action = Press(key=args["key"], target=provisional)
                return action, action, Sensitivity.INTERNAL
            case "extract":
                action = Extract(target=provisional, into=args["output_name"])
                return action, action, self.goal.outputs[args["output_name"]].sensitivity
            case "fill" | "select":
                template = self.recorder.parameterize(args["value" if verb == "fill" else "option"])
                value = render_template(template, inputs=examples)
                referenced = [self.goal.inputs[n].sensitivity
                              for n in sorted(template_refs(template, "inputs"))
                              if n in self.goal.inputs]
                sensitivity = next((s for s in referenced if s.masked_in_logs),
                                   Sensitivity.INTERNAL)
                if verb == "fill":
                    return (Fill(target=provisional, value=template),
                            Fill(target=provisional, value=value), sensitivity)
                return (Select(target=provisional, option=template),
                        Select(target=provisional, option=value), sensitivity)
        raise AssertionError(verb)

    def _request_help(self, reason: str) -> _Outcome:
        intervention = self.session.request_intervention(reason)
        if intervention.resolution == "aborted":
            return _Outcome(terminal=DiscoveryResult("aborted", f"operator aborted: {reason}"))
        obs = self.session.observe()
        self.state = classify(self.app, obs).state
        self.recorder.settle_last(self.state)
        return _Outcome(f"The operator ({intervention.operator}) handed control back after "
                        f"{len(intervention.actions)} action(s).\n\n{self._screen(obs)}")

    def _finish(self, args: dict[str, Any]) -> _Outcome:
        outcomes = {slug(o["code"]): OutcomeSpec(description=o["description"],
                                                 when_state=o["state"])
                    for o in args["outcomes"]}
        try:
            capability = self.recorder.build(list(args["steps"]), outcomes,
                                             run_id=self.session.run_id, model=self.planner.model)
        except RecordingError as e:
            return _Outcome("Cannot finish yet:\n- " + "\n- ".join(e.problems), is_error=True)
        self.session.log.write_json("capability.draft.json",
                                    capability.model_dump(mode="json"))
        return _Outcome(terminal=DiscoveryResult("recorded", "flow recorded",
                                                 capability=capability, notes=args["notes"]))

    # presentation -----------------------------------------------------------------------

    def _task(self, obs: Observation) -> str:
        lines = [f"Goal: {self.goal.goal}", ""]
        if self.goal.inputs:
            lines.append("Inputs (type the placeholder, not the example value):")
            lines += [f"- {{{{inputs.{n}}}}}: {s.description} (example for this run: "
                      f"{s.example})" for n, s in self.goal.inputs.items()]
            lines.append("")
        if self.goal.outputs:
            lines.append("Outputs to extract:")
            lines += [f"- {n} ({s.type}): {s.description}" for n, s in self.goal.outputs.items()]
            lines.append("")
        return "\n".join(lines) + self._screen(obs)

    def _screen(self, obs: Observation) -> str:
        sig = self.app.states.get(self.state) if self.state else None
        if sig is None:
            label = "unrecognised (no known screen matches)"
        else:
            label = f"{self.state} - {sig.description}"
            if sig.kind is StateKind.INTERSTITIAL and sig.recovery:
                label += f" [interstitial; replay handles it by: {sig.recovery.kind}]"
        self._screens_sent[obs.snapshot] = snapshot_digest(obs)
        return f"Screen: {label}\nURL: {obs.url}\n\n{obs.snapshot}"

    def _without_screens(self, transcript: Any) -> Any:
        """The transcript as evidence: the model's reasoning and calls, but each screen tree
        it was shown replaced by that observation's digest in the event log. Screens carry
        whatever data was on them, declared sensitive or not; the model has to see them,
        the evidence doesn't have to keep them."""
        match transcript:
            case str():
                for snapshot, digest in self._screens_sent.items():
                    transcript = transcript.replace(
                        snapshot, f"[screen tree omitted; observation {digest} in events.jsonl]")
                return transcript
            case dict():
                return {k: self._without_screens(v) for k, v in transcript.items()}
            case list():
                return [self._without_screens(v) for v in transcript]
        return transcript

    def _review_notes(self, capability: Capability | None) -> list[str]:
        notes = [f"example value for inputs.{n} was typed literally; recorded as a placeholder"
                 for n in dict.fromkeys(self.recorder.rewrites)]
        if capability is None:
            return notes
        chosen = {s.id for s in capability.steps}
        if skipped := [s for s in self.recorder.candidates if s not in chosen]:
            notes.append(f"recorded but left out of the flow: {skipped}")
        notes += [f"step {s.id} is recorded as {s.risk}; lower it to read_only at review if it "
                  "only queries" for s in capability.steps if s.risk.value == "reversible"]
        notes += [f"step {s.id} has no checkpoint (the screen after it was not recognised)"
                  for s in capability.steps
                  if s.expect is None and not isinstance(s.action, Fill | Select | Extract)]
        return notes
