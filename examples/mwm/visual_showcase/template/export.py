"""context_package: the five-part MWM package that interface.context() renders, as a tool result for the host."""
from .interface import context


def context_package(_resources=None) -> dict:
    return {"text": context(_resources=_resources)}
