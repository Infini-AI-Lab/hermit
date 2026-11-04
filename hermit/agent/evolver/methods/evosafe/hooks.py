"""EvoSafe's portable hook SDK, for defense bundles of any task bridge.

Subclass Defense to opt into structured hooks (hook_api = "structured-v1"); a bridge
also accepts the legacy dict hooks. Contexts carry public tool schemas and the trusted
task instruction, never benchmark labels, hidden state or judge predicates. This is not
DTAP's module: DTAP bundles need an explicit port.
"""
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolCallContext:
    framework: str
    server: str
    tool_name: str
    arguments: dict
    trace_id: str | None = None
    metadata: dict = field(default_factory=dict)
    classify_output: Callable[[str], dict] | None = field(default=None, repr=False, compare=False)


@dataclass
class ToolCallResult:
    raw: Any = None
    is_error: bool = False
    duration: float = 0.0
    exception: BaseException | None = None
    # A bridge whose tools catch their exceptions keeps only their text here,
    # without inventing an exception type.
    error: str | None = None


class Defense:
    hook_api = "structured-v1"

    def system_prompt_transform(self, prompt):
        return prompt

    def on_pre_tool_call(self, ctx):
        return None

    def on_post_tool_call(self, ctx, result):
        return None
