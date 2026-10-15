"""Admission test; never run in the host wrapper process."""
import run
assert run.SCHEMA["name"] != "<tool_name>"
