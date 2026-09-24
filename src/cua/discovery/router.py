"""From a message typed into the workbench chat to what to do about it: run a saved
automation (with the inputs the message gives), set up a new one, or just reply.

The model only chooses; it never touches the application, and it never sees member data.
Values in the message (numbers, amounts, emails) are taken out locally before anything is
sent (`take_values`) and put back into the run form here, so the model reads "the balance
for member «v1»" and the person sees 45678 filled in. Without an API key, `KeywordRouter`
matches by words instead, so the chat still finds saved automations.
"""

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from cua.schema.common import Model

from .contract import DEFAULT_MODEL

# Most specific first: an email or a card number is one value, not several numbers.
_VALUES = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"
                     r"|\b\d{3}-\d{2}-\d{4}\b"
                     r"|\b(?:\d[ -]?){12,19}\b"
                     r"|\$?\d(?:[\d,]*\d)?(?:\.\d+)?")
_PLACEHOLDER = re.compile(r"«v\d+»")


def take_values(text: str) -> tuple[str, dict[str, str]]:
    """`text` with every value replaced by a placeholder («v1», «v2», ...), and the values."""
    values: dict[str, str] = {}

    def keep(m: re.Match[str]) -> str:
        placeholder = f"«v{len(values) + 1}»"
        values[placeholder] = m.group(0).strip()
        return placeholder

    return _VALUES.sub(keep, text), values


class Route(Model):
    action: Literal["run", "create", "reply"]
    reply: str
    automation: str | None = None  # for "run": a saved automation's id
    inputs: dict[str, str] = {}  # for "run": what the message gives, placeholders restored
    request: str | None = None  # for "create": what to automate, without values


class Router(Protocol):
    def __call__(self, message: str, automations: Sequence[Mapping[str, Any]]) -> Route: ...


def to_route(raw: Mapping[str, Any], automations: Sequence[Mapping[str, Any]],
             redacted: str, values: Mapping[str, str]) -> Route:
    """Keep only what can be trusted: a saved automation, its own input names, and values
    that are placeholders or words from the message; put the real values back."""
    known = {a["id"]: a for a in automations}
    action = raw.get("action") if raw.get("action") in ("run", "create", "reply") else "reply"
    reply = _without_ids(str(raw.get("reply") or "").strip(), known)
    if action == "run" and raw.get("automation") not in known:
        action, reply = "reply", reply or "I couldn't match that to a saved automation."
    if action != "run":
        request = str(raw.get("request") or "").strip() or None
        if request and _VALUES.search(_PLACEHOLDER.sub("", request)):
            request = None  # a request carrying values isn't passed on
        return Route(action=action, reply=reply, request=request if action == "create" else None)
    automation = known[raw["automation"]]
    inputs: dict[str, str] = {}
    for item in raw.get("inputs") or []:
        name, value = str(item.get("name", "")), str(item.get("value", "")).strip()
        if name not in automation["inputs"] or not value:
            continue
        if value in values:
            inputs[name] = values[value]
        elif not _PLACEHOLDER.search(value) and value.lower() in redacted.lower():
            inputs[name] = value  # e.g. a product named in the message
    return Route(action="run", reply=reply, automation=automation["id"], inputs=inputs)


def _without_ids(reply: str, known: Mapping[str, Any]) -> str:
    """Staff read titles, not identifiers: drop any automation id the model mentioned
    (with the brackets, quotes or parentheses around it)."""
    for automation_id in known:
        reply = re.sub(rf"\s*\(?[«`'\"]*{re.escape(automation_id)}[»`'\"]*\)?", "", reply)
    return reply.strip()


SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["action", "reply", "automation", "inputs", "request"],
    "properties": {
        "action": {"type": "string", "enum": ["run", "create", "reply"]},
        "reply": {"type": "string", "description": "One or two short sentences to the person"},
        "automation": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "inputs": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "value"],
            "properties": {"name": {"type": "string"}, "value": {"type": "string"}}}},
        "request": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
}

SYSTEM = """\
You are the front desk of a bank's back-office automation workbench. A staff member types \
what they need; you decide what happens next. You never do the work yourself.

- action "run": a saved automation does what they ask and gives back everything they asked \
for. Give its id as automation, and for \
each of its inputs the message supplies, the value exactly as it appears in the message. \
Values appear as placeholders like «v1»; copy them unchanged. Leave out inputs the message \
doesn't give: the person fills them in.
- action "create": nothing saved does all of it (one that only covers part of it doesn't \
count: say which comes close in reply), but it is work in the teller system. Give request: \
what the new automation should do, in plain words, without any values or placeholders.
- action "reply": anything else (a question, a greeting, something outside the system). \
Answer briefly; if they ask what you can do, name the saved automations.
Keep reply short and plain. For "run", nothing has run yet: the person checks the details \
and presses Run, so say what you found (e.g. "That's the balance lookup."), not that it is \
running. In reply, name automations by what they do, never by their id. Never invent \
values."""


class ClaudeRouter:
    BETAS = ["server-side-fallback-2026-07-01"]

    def __init__(self, client: Any = None, *, model: str = DEFAULT_MODEL,
                 effort: str = "low") -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.effort = effort

    def __call__(self, message: str, automations: Sequence[Mapping[str, Any]]) -> Route:
        redacted, values = take_values(message)
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": (
                f"Saved automations:\n{json.dumps(list(automations), indent=1)}\n\n"
                f"Message:\n{redacted}")}],
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort,
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=self.BETAS,
            fallbacks="default",
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            return Route(action="reply", reply="I couldn't work that out; try rephrasing, or "
                                               "pick a saved automation below.")
        text = next((b.text for b in response.content if b.type == "text"), "{}")
        try:
            return to_route(json.loads(text), automations, redacted, values)
        except (ValueError, ValidationError):
            return Route(action="reply", reply="I couldn't work that out; try rephrasing.")


_STOP = set("a an the and or of for to in on at by with from my me i we our is are be it "
            "this that what whats what's which who please can could you do does show tell "
            "get find give need want member members".split())


def _words(text: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z]+", text.lower())
            if w not in _STOP and len(w) > 2}


class KeywordRouter:
    """No model: the saved automation whose title and description share the most words
    with the message, with the message's values given to its inputs in order."""

    def __call__(self, message: str, automations: Sequence[Mapping[str, Any]]) -> Route:
        redacted, values = take_values(message)
        asked = _words(redacted)
        scored = sorted(((len(asked & _words(f"{a['title']} {a['description']}")), a)
                         for a in automations), key=lambda pair: -pair[0])
        # two shared words, or one that points to a single automation
        clear = scored and (scored[0][0] >= 2 or (scored[0][0] == 1 and (
            len(scored) == 1 or scored[1][0] == 0)))
        if not clear:
            return Route(action="reply", reply=(
                "I couldn't match that to a saved automation. Pick one below; describing a "
                "new one needs the assistant (an API key)."))
        automation = scored[0][1]
        free = [name for name, spec in automation["inputs"].items()
                if not spec.get("enum")]
        inputs = dict(zip(free, values.values(), strict=False))
        return Route(action="run", automation=automation["id"], inputs=inputs,
                     reply=f"This looks like \"{automation['title']}\" (matched by its words).")
