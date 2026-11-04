"""Codex defense roles through the shared Base Agent API."""
from .cli_roles import call_role as _call_role


def proposal_schema():
    return {"type": "object", "additionalProperties": False, "required": ["summary", "files"],
              "properties": {"summary": {"type": "string"}, "files": {
                  "type": "object", "additionalProperties": False,
                  "required": ["ADAPTER.md", "defense.py", "BUNDLE.md"],
                  "properties": {name: {"type": "string"} for name in ("ADAPTER.md", "defense.py", "BUNDLE.md")}}}}


def propose(ctx, messages):
    return call_role(ctx, messages, proposal_schema(), "proposal")


def call_role(ctx, messages, schema, name):
    return _call_role(ctx, messages, schema, name, "codex")
