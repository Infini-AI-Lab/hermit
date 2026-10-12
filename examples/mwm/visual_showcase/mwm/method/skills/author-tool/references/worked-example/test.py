"""Admission check: raises on failure. Checks the SUBSTRATE (the module
imports, run() answers both ways), not a conclusion about any input."""
import run

assert run.SCHEMA["name"] == "json_check"
assert run.run(text='{"a": 1}') == {"valid": True}
assert run.run(text='{"a": ')["valid"] is False
