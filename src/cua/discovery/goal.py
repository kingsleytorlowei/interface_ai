"""What a human asks discovery to find: a goal in plain language plus the typed contract
(inputs with example values, outputs). Humans decide the contract; the LLM finds where it
lives in the UI.
"""

from pydantic import Field, model_validator

from cua.schema import InputSpec, OutputSpec
from cua.schema.common import DottedId, Model, Slug


class GoalInput(InputSpec):
    example: str  # the value used during discovery (and the first verification replay)
    # A second, different value for a second verification replay: a recorded fallback that
    # only works for the first example leans on that example's data and is dropped. Inputs
    # without one reuse `example`.
    alt_example: str | None = None


class Goal(Model):
    capability_id: DottedId
    goal: str
    title: str | None = None  # a short name for people; the capability's description
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

    def verification_params(self) -> list[dict[str, str]]:
        """One replay per example set: the discovery examples, then the alternates if any."""
        sets = [self.examples()]
        if any(spec.alt_example for spec in self.inputs.values()):
            sets.append({name: spec.alt_example or spec.example
                         for name, spec in self.inputs.items()})
        return sets
