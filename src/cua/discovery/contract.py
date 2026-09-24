"""From a request in plain English to the typed contract a goal needs: what the automation
takes, what it gives back, and how sensitive each is. Claude proposes; a person confirms or
corrects it before discovery starts, because the contract is what callers rely on.

The proposal never carries values. Example values are member data, so a person types them
in afterwards; a request that already contains something that looks like one is refused
before it is sent (`sensitive_looking`), and any value that still comes back is removed.
"""

import json
import re
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import ValidationError

from cua.schema import InputSpec, OutputSpec, ParamType, Sensitivity
from cua.schema.common import DottedId, Model, Slug

DEFAULT_MODEL = "claude-opus-5"

# Things that look like member data: account or member numbers, card numbers, SSNs, emails.
_SENSITIVE = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b(?:\d[ -]?){12,19}\b"),
    re.compile(r"\b\d{4,}\b"),
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
]


def sensitive_looking(text: str) -> list[str]:
    """Fragments of `text` that look like member data and must not be sent to the model."""
    return sorted({m.group(0).strip() for p in _SENSITIVE for m in p.finditer(text)})


class ContractProposal(Model):
    title: str
    capability_id: DottedId
    inputs: dict[Slug, InputSpec]
    outputs: dict[Slug, OutputSpec]
    questions: list[str] = []  # what the person should check before accepting
    warnings: list[str] = []  # what was changed in the model's proposal, and why


class ProposalError(Exception):
    pass


class ContractProposer(Protocol):
    def __call__(self, request: str, app_id: str, app_description: str,
                 examples: Sequence[dict[str, Any]]) -> ContractProposal: ...


_SENSITIVITY = ["public", "internal", "pii", "confidential"]  # never "secret"
_TYPES = [t.value for t in ParamType]
_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _field(extra: dict[str, Any]) -> dict[str, Any]:
    properties = {
        "name": {"type": "string", "description": "snake_case, e.g. member_id"},
        "type": {"type": "string", "enum": _TYPES},
        "description": {"type": "string", "description": "What it is, for a bank employee"},
        "sensitivity": {"type": "string", "enum": _SENSITIVITY},
        **extra,
    }
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": list(properties)}


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "name", "inputs", "outputs", "questions"],
    "properties": {
        "title": {"type": "string", "description": "Short name, under 60 characters"},
        "name": {"type": "string",
                 "description": "Dotted snake_case identifier without the app prefix: "
                                "<area>.<what>, e.g. member.savings_account_number"},
        "inputs": {"type": "array", "items": _field({
            "pattern": {**_NULLABLE_STRING,
                        "description": "A regular expression, only if the format is certain"},
            "choices": {"anyOf": [{"type": "array", "items": {"type": "string"}},
                                  {"type": "null"}],
                        "description": "A fixed list of options, only if the request names "
                                       "them (e.g. product names)"},
        })},
        "outputs": {"type": "array", "items": _field({})},
        "questions": {"type": "array", "items": {"type": "string"},
                      "description": "Anything ambiguous the person should confirm"},
    },
}

SYSTEM = """\
You turn a bank employee's request into the typed contract of a reusable automation for a \
back-office application. Another step will later find how to do it in the application; you \
only decide what the automation takes and what it gives back.

- Inputs are what changes from one run to the next and the person running it supplies \
(e.g. a member number, an amount, a product). Outputs are what it reads back from the screen.
- Never include example values, names, numbers or any member data anywhere in your answer; \
the person adds example values themselves.
- Types: string, integer, decimal, money (amounts of currency), boolean, date.
- Sensitivity: pii for anything identifying a person (member numbers, names, addresses); \
confidential for balances, account numbers and other financial data; internal for product \
names and codes; public otherwise.
- Give a pattern only when the request states the format; give choices only when the request \
names them. Put anything you had to guess into questions, in plain words.
- Follow the naming style of the existing automations shown."""


class ClaudeContractProposer:
    """One structured-output call: the answer is JSON matching SCHEMA, then validated into a
    ContractProposal here."""

    BETAS = ["server-side-fallback-2026-07-01"]

    def __init__(self, client: Any = None, *, model: str = DEFAULT_MODEL,
                 effort: str = "medium") -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.effort = effort

    def __call__(self, request: str, app_id: str, app_description: str,
                 examples: Sequence[dict[str, Any]]) -> ContractProposal:
        if found := sensitive_looking(request):
            raise ProposalError("the request contains what looks like member data "
                                f"({', '.join(found)}); remove it and give example values "
                                "in the next step instead")
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": (
                f"Application: {app_id}. {app_description}\n\n"
                f"Existing automations, for naming style:\n{json.dumps(list(examples), indent=1)}"
                f"\n\nRequest:\n{request}")}],
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort,
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=self.BETAS,
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise ProposalError("the model declined to propose a contract for this request")
        if response.stop_reason == "max_tokens":
            raise ProposalError("the proposal was cut off; try a shorter request")
        text = next((b.text for b in response.content if b.type == "text"), "")
        return to_proposal(json.loads(text), app_id, request)


def to_proposal(raw: dict[str, Any], app_id: str, request: str) -> ContractProposal:
    """Validate the model's JSON into a proposal, removing anything that isn't allowed:
    secrets, values that appear in the request, malformed names."""
    warnings: list[str] = []
    request_values = set(sensitive_looking(request))

    def clean(items: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
        fields: dict[str, dict[str, Any]] = {}
        for item in items:
            name = re.sub(r"[^a-z0-9_]", "_", str(item.get("name", "")).lower()).strip("_")
            if not name or not name[0].isalpha():
                warnings.append(f"dropped an {kind} without a usable name")
                continue
            spec: dict[str, Any] = {"type": item.get("type", "string"),
                                    "description": item.get("description") or name,
                                    "sensitivity": item.get("sensitivity", "internal")}
            if spec["sensitivity"] not in _SENSITIVITY:
                spec["sensitivity"] = Sensitivity.CONFIDENTIAL.value
            if kind == "input":
                choices = [c for c in item.get("choices") or [] if c not in request_values]
                if spec["type"] == ParamType.STRING.value:
                    if choices:
                        spec["enum"] = choices
                    if pattern := item.get("pattern"):
                        try:
                            re.compile(pattern)
                            spec["pattern"] = pattern
                        except re.error:
                            warnings.append(f"dropped the format rule for {name}: it isn't "
                                            "a valid pattern")
            fields[name] = spec
        return fields

    try:
        name = str(raw.get("name", "")).removeprefix(f"{app_id}.")
        return ContractProposal(
            title=str(raw.get("title", "")).strip()[:80] or name,
            capability_id=f"{app_id}.{name}",
            inputs={k: InputSpec.model_validate(v)
                    for k, v in clean(raw.get("inputs", []), "input").items()},
            outputs={k: OutputSpec.model_validate(v)
                     for k, v in clean(raw.get("outputs", []), "output").items()},
            questions=[str(q) for q in raw.get("questions", [])],
            warnings=warnings,
        )
    except ValidationError as e:
        raise ProposalError(f"the proposal isn't usable: {e.errors()[0]['msg']}") from e
