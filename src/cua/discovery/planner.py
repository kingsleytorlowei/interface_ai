"""The decision-maker in the discovery loop, behind a small port so the loop can be tested
without a model: `ClaudePlanner` calls the Claude API; `ScriptedPlanner` replays a fixed
list of tool calls (resolving element refs from the latest snapshot).
"""

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MODEL = "claude-opus-5"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Turn:
    calls: list[ToolCall]
    text: str = ""
    stop_reason: str = "tool_use"
    usage: dict[str, int] = field(default_factory=dict)


class Planner(Protocol):
    model: str

    def begin(self, system: str, tools: list[dict[str, Any]], task: str) -> Turn: ...

    def respond(self, results: list[ToolResult], note: str | None = None) -> Turn:
        """Send tool results (and optionally a text note) and get the next turn."""

    def transcript(self) -> list[dict[str, Any]]: ...


# --- Claude ---------------------------------------------------------------------------------


class ClaudePlanner:
    """Manual tool-use loop over the Messages API (beta surface for context editing and
    server-side refusal fallback).

    - Frozen system prompt and tool list, auto-cached; history is append-only so the cache
      prefix and thinking blocks stay valid.
    - Old tool results (page snapshots) are cleared server-side by context editing rather than
      by rewriting history here.
    - One tool call per turn; the loop must see each action's effect before the next.
    """

    BETAS = ["context-management-2025-06-27", "server-side-fallback-2026-07-01"]

    def __init__(self, client: Any = None, *, model: str = DEFAULT_MODEL, effort: str = "high",
                 max_tokens: int = 16000, keep_tool_uses: int = 4) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.keep_tool_uses = keep_tool_uses
        self._system = ""
        self._tools: list[dict[str, Any]] = []
        self._messages: list[dict[str, Any]] = []

    def begin(self, system: str, tools: list[dict[str, Any]], task: str) -> Turn:
        self._system, self._tools = system, tools
        self._messages = [{"role": "user", "content": task}]
        return self._call()

    def respond(self, results: list[ToolResult], note: str | None = None) -> Turn:
        content: list[dict[str, Any]] = [
            {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content,
             "is_error": r.is_error}
            for r in results
        ]
        if note:
            content.append({"type": "text", "text": note})
        self._messages.append({"role": "user", "content": content})
        return self._call()

    def _call(self) -> Turn:
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self._system,
            tools=self._tools,
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            messages=self._messages,
            cache_control={"type": "ephemeral"},
            betas=self.BETAS,
            fallbacks="default",
            context_management={"edits": [{
                "type": "clear_tool_uses_20250919",
                "trigger": {"type": "input_tokens", "value": 40_000},
                "keep": {"type": "tool_uses", "value": self.keep_tool_uses},
            }]},
        )
        # Append the full content (thinking and tool_use blocks), never just the text.
        self._messages.append({"role": "assistant", "content": response.content})
        usage = response.usage
        return Turn(
            calls=[ToolCall(b.id, b.name, dict(b.input))
                   for b in response.content if b.type == "tool_use"],
            text="".join(b.text for b in response.content if b.type == "text"),
            stop_reason=response.stop_reason or "",
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_input_tokens": usage.cache_read_input_tokens or 0,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens or 0,
            },
        )

    def transcript(self) -> list[dict[str, Any]]:
        def plain(block: Any) -> Any:
            return block.model_dump(mode="json") if hasattr(block, "model_dump") else block

        return [{"role": m["role"], "content": m["content"] if isinstance(m["content"], str)
                 else [plain(b) for b in m["content"]]} for m in self._messages]


# --- scripted (tests, offline demo) ---------------------------------------------------------

_REF_LINE = re.compile(
    r'- (?P<role>[\w-]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?[^\n]*?\[ref=(?P<ref>\w+)\]'
)


def find_ref(snapshot: str, role: str, name: str | None = None, nth: int = 0) -> str:
    """Ref of the nth element with this role (and exact accessible name, if given)."""
    refs = [m["ref"] for m in _REF_LINE.finditer(snapshot)
            if m["role"] == role and (name is None or (m["name"] or "") == name)]
    if len(refs) <= nth:
        raise LookupError(f"no {role} {name!r} (#{nth}) in snapshot")
    return refs[nth]


class ScriptedPlanner:
    """Plays a fixed script. An input value `("role", "name")` or `("role", None, n)` for
    `ref` is resolved against the latest snapshot the loop sent back."""

    model = "scripted"

    def __init__(self, script: list[tuple[str, dict[str, Any]]]) -> None:
        self._script = list(script)
        self._last = ""
        self._n = 0
        self.received: list[ToolResult] = []

    def begin(self, system: str, tools: list[dict[str, Any]], task: str) -> Turn:
        self._last = task
        return self._next()

    def respond(self, results: list[ToolResult], note: str | None = None) -> Turn:
        self.received.extend(results)
        self._last = results[-1].content if results else self._last
        return self._next()

    def _next(self) -> Turn:
        if not self._script:
            return Turn(calls=[], text="(script exhausted)", stop_reason="end_turn")
        name, raw = self._script.pop(0)
        args = dict(raw)
        if isinstance(args.get("ref"), tuple):
            args["ref"] = find_ref(self._last, *args["ref"])
        self._n += 1
        return Turn(calls=[ToolCall(f"call_{self._n}", name, args)])

    def transcript(self) -> list[dict[str, Any]]:
        return [{"tool_result": r.content, "is_error": r.is_error} for r in self.received]
