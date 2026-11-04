"""Portable EvoSafe hook surface. No DTAP dependency; one instance per episode."""


class Defense:
    def system_prompt_transform(self, prompt):
        return prompt

    def on_pre_tool_call(self, ctx):
        # None: execute unchanged. {"arguments": {...}}: rewrite arguments.
        # {"block": "reason"}: refuse without invoking the tool.
        return None

    def on_post_tool_call(self, ctx, result):
        # result = {"text": serialized tool output, "error": str | None}.
        # None: unchanged; return a replacement result to redact output.
        return None


def build():
    return Defense()
