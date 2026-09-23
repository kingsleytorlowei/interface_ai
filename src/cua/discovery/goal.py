"""What a human asks discovery to find: a goal in plain language plus the typed contract
(inputs with example values, outputs). Humans decide the contract; the LLM finds where it
lives in the UI.
"""

from pydantic import Field, model_validator

from cua.schema import InputSpec, OutputSpec
from cua.schema.common import DottedId, Model, Slug


class GoalInput(InputSpec):
    example: str  # the value used during discovery (and the verification replay)


class Goal(Model):
    capability_id: DottedId
    goal: str
    app: Slug
    entry: str  # template, e.g. "{{env.base_url}}/main.html"
    requires_session: bool = True
    inputs: dict[Slug, GoalInput] = {}
    outputs: dict[Slug, OutputSpec] = {}
    max_turns: int = Field(default=30, gt=0)

    @model_validator(mode="after")
    def _namespaced(self) -> "Goal":
        if self.capability_id.split(".")[0] != self.app:
            raise ValueError(f"capability id must be namespaced by app {self.app!r}")
        return self

    def examples(self) -> dict[str, str]:
        return {name: spec.example for name, spec in self.inputs.items()}
