"""Claude defense roles through the shared Base Agent API."""
from .cli_roles import call_role as _call_role


def call_role(ctx, messages, schema, name):
    return _call_role(ctx, messages, schema, name, "claude")
