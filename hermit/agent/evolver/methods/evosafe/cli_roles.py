"""EvoSafe's role orchestration over the shared CLI Base Agents (`codex`, `claude`).

Each role is one fresh session of the configured CLI through `make(backend, settings).run(...)`; the Base Agent
owns the process, the login plan, the timeout, cancellation and usage. The structured answer is checked here,
in Layer 3, from the session's reply and its standard trajectory steps (never from the raw CLI log).
"""
from dataclasses import asdict
import json
import re

from hermit.agent.base.interface import Limits, make
from .experience import role_tools

# The Layer 1/2 settings a configuration may give the role CLI; the rest are set per call below.
CLI_SETTINGS = ("model", "binding", "login", "login_root", "plan_dir", "plan_max_wait_s", "plan_rerun", "effort",
                "isolation", "container", "codex_sandbox", "permission_mode", "shell_env", "project_doc_max_bytes",
                "home_files", "args", "env", "env_drop", "price", "timeout_s", "bin")

# Provider messages that mean the role request itself was refused (stop, never retried).
REFUSAL = ("usage policy", "content_policy_violation", "flagged for possible cybersecurity risk")


def proposer(params):
    """Layered config takes precedence over the legacy method-specific selector."""
    return params.get("backend") or params.get("proposer", "llm")


def parse_answer(reply, schema):
    """The role's JSON object from its last message (a fenced block is accepted), or None when it does not
    satisfy `schema`."""
    import jsonschema
    text = (reply or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    try:
        value = json.loads(fenced.group(1) if fenced else text)
        jsonschema.validate(value, schema)
    except (ValueError, jsonschema.ValidationError):
        return None
    return value if isinstance(value, dict) else None


def refused(result):
    """A provider refusal of the role request, seen in the reply or in an error step of the trajectory."""
    texts = [result.reply or ""] + [step.text for step in result.trajectory if step.kind == "error"]
    return any(marker in text.lower() for text in texts for marker in REFUSAL)



def authentication_failed(result):
    texts = [result.reply or ""] + [step.text for step in result.trajectory if step.kind == "error"]
    return any(marker in text.lower() for text in texts for marker in
               ("failed to authenticate", "oauth session expired", "authentication_error", "invalid api key"))

def call_role(ctx, messages, schema, name, backend):
    if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in name):
        raise ValueError("invalid role artifact name")
    root = ctx.workspace.scratch / (backend + "-" + name)
    root.mkdir()
    params = ctx.params
    # Config has already resolved model bindings and flattened evolver.<base>; only the CLI's own settings
    # (hermit/agent/base/cli.py) reach the Base Agent, not the method's parameters.
    settings = {k: params[k] for k in CLI_SETTINGS if params.get(k) is not None}
    for key in ("bin", "model", "timeout_s"):
        if settings.get(key) is None and params.get(f"{backend}_{key}") is not None:
            settings[key] = params[f"{backend}_{key}"]
    settings.setdefault("timeout_s", 600 if backend == "codex" else 1200)
    settings.setdefault("isolation", "sandbox" if ctx.sandbox is not None else "none")
    settings.update(sandbox=ctx.sandbox, cancel=ctx.cancel, web_search=False, stdin=True,
                    log=ctx.workspace.output / f"{backend}-{name}-events.jsonl")
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    user = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
    tools = role_tools(ctx)
    instructions = ("Perform only the requested defensive role from the supplied evidence. "
                    "Do not read other project files, invoke APIs, execute shell commands, edit files, "
                    "or perform any git operations. "
                    + ("Use only list_training_experience and read_training_experience to inspect additional "
                       "registered TRAIN evidence; paginate to obtain complete observations.\n" if tools else
                       "All required information is below.\n"))
    answer_format = ("\nAnswer with one JSON object, and nothing else, that satisfies this JSON schema:\n"
                     + json.dumps(schema, ensure_ascii=False) + "\n")
    args = list(settings.get("args") or [])
    if backend == "claude":
        # No built-in tools (files, commands, browsing); TRAIN readers remain available over MCP.
        settings.update(args=args + ["--tools", ""], persist_session=False, append_system_prompt=system)
        prompt = instructions + answer_format + user
    else:
        # Codex enforces the schema on its last message; the session is ephemeral and read-only.
        (root / "output-schema.json").write_text(json.dumps(schema))
        settings.setdefault("model", "gpt-6-sol")
        settings.update(args=["-c", "daybreak=true"] + args + ["--output-schema", str(root / "output-schema.json"), "--ephemeral"])
        settings.setdefault("codex_sandbox", "read-only")
        prompt = instructions + json.dumps(messages, ensure_ascii=False)
    (root / "prompt.txt").write_text(prompt)
    result = make(backend, settings).run(prompt, workspace=root, tools=tools,
                                       limits=Limits(timeout_s=settings["timeout_s"]), session=None)
    (ctx.workspace.output / f"{backend}-{name}-trajectory.json").write_text(
        json.dumps([asdict(step) for step in result.trajectory], indent=2))
    if result.extra.get("cancelled"):
        return None, result.usage, f"{backend} role cancelled"
    if result.timed_out:
        return None, result.usage, f"{backend} role timed out"
    answer = parse_answer(result.reply, schema) if result.exit_code == 0 else None
    if answer is None:
        # Only a failed answer is read for login errors: a valid one may quote such text.
        if authentication_failed(result):
            return None, result.usage, "ROLE_AUTHENTICATION_ERROR: restore the configured CLI login before continuing"
        if refused(result):
            return None, result.usage, "SAFETY_REVIEW_REFUSAL: the provider refused the role request"
        return None, result.usage, f"{backend} role failed (exit {result.exit_code}); inspect {result.log}"
    text = json.dumps(answer)
    (ctx.workspace.output / f"{backend}-{name}.json").write_text(text)
    return {"content": text}, result.usage, None
