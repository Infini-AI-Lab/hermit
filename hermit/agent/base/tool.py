"""A tool, as Layer 3 defines it and every Base Agent offers it (docs/framework/agent-layers.md#tools).

A Tool is a name, a description, an input schema and the function that runs it. It always runs on the framework's
side: a Base Agent only offers it to the model (in the API request for direct and react, through the tool server for
the CLIs). Where the tool came from (the task, the adapter, the Trainer) is Layer 3's business.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator

LOG_LIMIT = 4000


class ToolError(RuntimeError):
    pass


def as_json_text(result: Any) -> str:
    """A result as JSON text, the way a model reads it by default."""
    return json.dumps(result, ensure_ascii=False, default=str)


class Tool:
    """`fn(**arguments)` behind a name, a description and a JSON Schema of its arguments.

    `validate` checks the arguments against the schema before the call; an `output_schema` of type object requires a
    JSON object back. `render` turns a result into the text a model reads. Every call is appended to `log_path` when
    one is set, and refused once `cancel` (a threading.Event) is set."""

    def __init__(self, name: str, description: str, fn: Callable[..., Any], *, source: str = "task",
                 input_schema: dict | None = None, output_schema: dict | None = None, validate: bool = True,
                 render: Callable[[Any], str] = as_json_text):
        self.name, self.description, self.fn, self.source = name, description, fn, source
        self.input_schema = input_schema or {"type": "object"}
        self.output_schema = output_schema if output_schema is not None else {"type": "object"}
        Draft202012Validator.check_schema(self.input_schema)
        self.validate, self.render = validate, render
        self.log_path: Path | None = None
        self.cancel = None

    def describe(self) -> dict:
        return {"name": self.name, "description": self.description, "source": self.source,
                "input_schema": self.input_schema, "output_schema": self.output_schema}

    def openai_spec(self) -> dict:
        """The tool as the chat-completions API takes it."""
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.input_schema}}

    def __call__(self, **arguments: Any) -> Any:
        record = {"time": datetime.now(timezone.utc).isoformat(), "tool": self.name, "source": self.source,
                  "arguments": _clip(arguments)}
        try:
            if self.cancel is not None and self.cancel.is_set():
                raise ToolError("the call was stopped by the Trainer (budget or timeout)")
            if self.validate:
                Draft202012Validator(self.input_schema).validate(arguments)
            result = self.fn(**arguments)
            if (self.output_schema or {}).get("type") == "object" and not isinstance(result, dict):
                raise ToolError(f"tool {self.name} returned {type(result).__name__}, not an object")
            record.update(status="ok", result=_clip(result))
            return result
        except Exception as exc:
            record.update(status="error", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if self.log_path is not None:
                with open(self.log_path, "a") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def answer(self, arguments: Any, max_chars: int | None = None) -> str:
        """Run one call from a model and return the text it reads: the rendered result, or the error. Arguments
        arrive as JSON text or an object; nothing raises, so an agent's loop goes on."""
        try:
            if isinstance(arguments, str):
                arguments = json.loads(arguments or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
            text = self.render(self(**arguments))
        except Exception as exc:
            text = f"Error: {type(exc).__name__}: {exc}"
        return text[:max_chars] if max_chars is not None else text


def _clip(value):
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= LOG_LIMIT else text[:LOG_LIMIT] + "…"
