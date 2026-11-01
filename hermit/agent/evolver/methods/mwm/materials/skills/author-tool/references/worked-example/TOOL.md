Check that a text is valid JSON and report the first error position.
scope: any text a host can pass as an argument
last_confirmed_t: 0
derived_from: none (worked example shipped with the skill)

## Invocation

    wm(name="json_check", args={"text": "<the JSON text>"})

Returns {"valid": true} or {"valid": false, "line": L, "col": C, "reason": "..."}.

## Verification

    test.py passes in the sandbox (admission); wm(name="json_check",
    args={"text": "{}"}) returns {"valid": true}.
