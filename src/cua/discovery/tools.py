"""Tool definitions offered to the model, generated from the goal so enums are exact.

All tools are strict (inputs are guaranteed to match the schema) and the loop allows one
call per turn: every action changes the page, so the model must see the result first.
"""

from typing import Any

from cua.schema import AppModel, StateKind

from .goal import Goal

KEYS = ["Enter", "Tab", "Escape"]


def _tool(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


REF = {"type": "string", "description": "Element ref from the latest snapshot, e.g. f3e12"}
TARGET_NAME = {
    "type": "string",
    "description": "Stable snake_case name for this element in the recorded flow, "
                   "e.g. member_id_field. Reuse the same name for the same element.",
}
INTENT = {"type": "string", "description": "What this action accomplishes, in a few words"}


def tool_definitions(goal: Goal, app: AppModel) -> list[dict[str, Any]]:
    screens = sorted(n for n, s in app.states.items() if s.kind is StateKind.SCREEN)
    tools = [
        _tool("click", "Click an element.",
              {"ref": REF, "target_name": TARGET_NAME, "intent": INTENT}),
        _tool("fill", "Type into a text field (replaces its contents). Use {{inputs.<name>}} "
                      "for any goal input rather than its example value.",
              {"ref": REF, "target_name": TARGET_NAME, "value": {"type": "string"},
               "intent": INTENT}),
        _tool("select", "Choose an option (by its visible label) in a dropdown. Use "
                        "{{inputs.<name>}} for any goal input.",
              {"ref": REF, "target_name": TARGET_NAME, "option": {"type": "string"},
               "intent": INTENT}),
        _tool("press", "Press a key while an element is focused.",
              {"ref": REF, "target_name": TARGET_NAME, "key": {"type": "string", "enum": KEYS},
               "intent": INTENT}),
        _tool("request_help", "Hand the live session to a human operator, e.g. for a screen "
                              "that says a person must act. Returns when they hand back.",
              {"reason": {"type": "string"}}),
        _tool("finish", "Finish discovery. List the ids of the recorded steps that make up the "
                        "flow, in order (leave out detours). Map business outcomes you saw or "
                        "expect to screen states.",
              {"steps": {"type": "array", "items": {"type": "string"}},
               "outcomes": {"type": "array", "items": {
                   "type": "object",
                   "properties": {"code": {"type": "string"},
                                  "state": {"type": "string", "enum": screens},
                                  "description": {"type": "string"}},
                   "required": ["code", "state", "description"],
                   "additionalProperties": False}},
               "notes": {"type": "string"}}),
        _tool("give_up", "Stop without a result, explaining why.", {"reason": {"type": "string"}}),
    ]
    if goal.outputs:
        tools.insert(4, _tool(
            "extract", "Read an element's text into one of the goal's outputs.",
            {"ref": REF, "output_name": {"type": "string", "enum": sorted(goal.outputs)},
             "intent": INTENT}))
    return tools
