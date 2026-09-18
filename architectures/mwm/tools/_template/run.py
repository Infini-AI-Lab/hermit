SCHEMA = {"name": "<tool_name>", "description": "<what it does, when to call it>",
          "parameters": {"type": "object", "properties": {}, "required": []}}

def run(**kwargs):
    """Stdlib-only. Executes only through the framework sandbox resource."""
    raise NotImplementedError
