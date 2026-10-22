"""One job, one result object. A negative verdict is a value; an exception
is reserved for 'could not run', so the caller can tell answer from breakage."""
import json

SCHEMA = {
    "name": "json_check",
    "description": "Check whether a text is valid JSON. Returns {valid: true}, "
                   "or {valid: false, line, col, reason} for the first error.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The JSON text to check."},
        },
        "required": ["text"],
    },
}


def run(text):
    try:
        json.loads(text)
    except json.JSONDecodeError as e:
        return {"valid": False, "line": e.lineno, "col": e.colno, "reason": e.msg}
    return {"valid": True}
