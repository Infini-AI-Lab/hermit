"""A Harbor trial's launch plan (hermit.harbor.launch) and the per-CLI table (hermit.harbor.clis), without Docker or
Harbor: what each CLI's plan holds as Hermit's own CLI Base Agents render it, that no secret's value and no path of
this machine reaches the container, the sign-ins (ChatGPT's file that comes back, Claude's token on a descriptor),
the memory note, the mini-swe-agent bundle's imports, the answer agent's plan and the plan's digest."""
import ast
import json
import os
from pathlib import Path
import sys

import pytest

from hermit.harbor import clis, launch

KEY_ENV, KEY = "FAKE_MODEL_KEY", "fake-model-key-0123456789abcdef"
TOKEN = "fake-setup-token-never-real"
BINDING = {"ref": "fake/glm-5", "backend": "fake", "model": "glm-5", "base_url": "https://models.example/v1",
           "api_key_env": KEY_ENV, "key_required": True, "apis": ["chat", "responses", "messages"],
           "price": {"input": 1.0, "output": 2.0}}
CLIS = ("codex", "claude", "pi", "opencode", "dsh", "mini-swe-agent")
URL = "http://172.17.0.1:41234/" + "ab" * 16 + "/mcp"     # a tool server's MCP URL (its token: 32 hex characters)


@pytest.fixture(autouse=True)
def key(monkeypatch):
    """The model's key is set: a plan names its variable, never its value."""
    monkeypatch.setenv(KEY_ENV, KEY)


def settings(cli: str, **kwargs) -> dict:
    from hermit.agent.model.backends import Binding
    return launch.settings_for(cli, binding=Binding.from_spec(BINDING), web_search=False, **kwargs)


def plan_of(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def launch_texts(job: Path) -> str:
    """Every file the launch left under the job (the plan, the home, the bundle), joined."""
    return "\n".join(p.read_text(errors="replace") for p in (job / "launch").rglob("*") if p.is_file())


def staged(root: Path, adapter_id: str = "demo", memory: bool = False) -> dict:
    """A staging record (hermit.harbor.staging.stage) of a deployment with the adapter's copy, and its notes."""
    deploy = root / "deploy"
    (deploy / "hermit" / adapter_id).mkdir(parents=True)
    (deploy / "AGENTS.md").write_text("# demo\n")
    if memory:
        (deploy / "hermit" / adapter_id / "memory").mkdir()
        (deploy / "hermit" / adapter_id / "memory" / "NOTES.md").write_text("- notes\n")
    return {"host": "codex", "adapter_id": adapter_id, "deploy": str(deploy), "hooks": False, "skills": [],
            "tools": ["calc"]}


# ---- the table ---------------------------------------------------------------------------------------------------

def test_the_table_names_one_class_per_cli():
    assert clis.cli_of({}) == "codex" and clis.cli_of(None) == "codex"
    assert clis.cli_of({"cli": "dsh"}) == "dsh"
    assert clis.cli_of({"import_path": "harborside.claude:HermitClaude"}) == "claude"
    assert clis.cli_of({"import_path": "harborside.pi:HermitPi"}) == "pi"
    assert clis.cli_of({"name": "terminus-2"}) is None
    assert clis.cli_of({"import_path": "my_agents:Agent"}) is None        # another class: Harbor's way, no plan
    with pytest.raises(ValueError, match="give one"):
        clis.cli_of({"cli": "codex", "name": "codex"})
    with pytest.raises(ValueError, match="one of"):
        clis.cli_of({"cli": "aider"})
    from hermit.agent.model.backends import HARBOR_ACTING              # the model layer's copy of the class names
    assert HARBOR_ACTING == {row["import_path"].partition(":")[2]: cli for cli, row in clis.HARBOR_CLIS.items()}
    assert clis.bin_mount("opencode") == "/opt/hermit-opencode"
    assert all(clis.entry(c)["context_file"] for c in ("codex", "claude", "pi", "opencode", "dsh"))


# ---- every CLI's plan --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cli", CLIS)
def test_every_cli_renders_a_plan_for_the_container(tmp_path, cli):
    job = tmp_path / "job"
    plan = plan_of(launch.render(cli, settings(cli), deploy=None, tools_url=URL, job=job, tools_name="demo"))
    backend = clis.entry(cli)["backend"]
    assert (plan["version"], plan["cli"], plan["stream"]) == (1, cli, f"agent-{backend}.jsonl")
    assert plan["metadata"] == {"hermit_cli": cli, "backend": backend}
    # The prompt goes on stdin; the home is the container's
    assert plan["stdin"] is True and launch.PROMPT not in json.dumps(plan["command"])
    # Claude Code runs a prompt that starts with `/` as one of its commands: its Base Agent's line break goes first
    assert plan["prompt_slash_prefix"] == ("\n" if cli == "claude" else None)
    assert plan["home"] == {"host_dir": str((job / "launch" / "home").resolve()), "container_dir": clis.CONTAINER_HOME,
                            "secret_files": []}
    # Nothing of this machine's job where the container reads it, and the key by its name only
    assert str(job.resolve()) not in json.dumps([plan["command"], plan["env"]])
    assert KEY not in launch_texts(job)
    expected = {"ANTHROPIC_AUTH_TOKEN": KEY_ENV} if cli == "claude" else {KEY_ENV: KEY_ENV}
    assert plan["env_from_host"] == expected and plan["env_by_fd"] == {}
    assert plan["nvm"] is clis.entry(cli)["nvm"] and plan["kill_grace_s"] == launch.KILL_GRACE_S
    # The tools' server is named where the CLI looks for it: its MCP entry, or for mini-swe-agent's shell the JSON one
    home = Path(plan["home"]["host_dir"])
    named = "\n".join(p.read_text(errors="replace") for p in home.rglob("*") if p.is_file())
    url = URL.removesuffix("/mcp") if cli == "mini-swe-agent" else URL
    assert url in named + json.dumps(plan["command"]) + json.dumps(plan["env"])
    assert plan["tools_script"] == ({"url_env": "HERMIT_TOOLS_URL"} if cli == "mini-swe-agent" else None)


def test_the_table_settings_and_the_runs_own_reach_the_cli(tmp_path):
    codex = plan_of(launch.render("codex", settings("codex", effort="high", extra={"args": ["-c", "x=1"]}),
                                  deploy=None, tools_url=None, job=tmp_path / "c"))
    command = codex["command"]
    assert command[:2] == ["codex", "exec"] and command[-1] == "-" and "-o" in command
    assert command[command.index("-s") + 1] == "danger-full-access"     # the container is the boundary
    assert 'model_reasoning_effort="high"' in command and "x=1" in command and 'web_search="disabled"' in command
    assert codex["collect"] == [{"from": f"{clis.CONTAINER_HOME}/last_message.txt", "to": "last_message.txt"}]
    assert command[command.index("-o") + 1] == f"{clis.CONTAINER_HOME}/last_message.txt"
    assert codex["env"]["CODEX_HOME"] == clis.CONTAINER_HOME
    claude = plan_of(launch.render("claude", settings("claude"), deploy=None, tools_url=None, job=tmp_path / "cl"))
    assert claude["env"]["IS_SANDBOX"] == "1" and claude["env"]["ANTHROPIC_BASE_URL"] == "https://models.example"
    opencode = plan_of(launch.render("opencode", settings("opencode"), deploy=None, tools_url=None,
                                     job=tmp_path / "o"))
    assert opencode["collect"] == [{"from": f"{clis.CONTAINER_HOME}/data/opencode/{name}", "to": name}   # WAL mode
                                   for name in ("opencode.db", "opencode.db-wal", "opencode.db-shm")]
    dsh = plan_of(launch.render("dsh", settings("dsh"), deploy=None, tools_url=None, job=tmp_path / "d"))
    assert "danger-full-access" in json.dumps(dsh) and dsh["nvm"] is True
    assert launch.context_limit("dsh", {}) == clis.entry("dsh")["context_limit"]
    assert launch.context_limit("codex", settings("codex")) == 262144 and launch.context_limit("pi", {}) is None


def test_codexs_plan_loads_the_agent_users_nvm_where_harbors_install_puts_codex(tmp_path):
    """Harbor's Codex install puts codex under the agent user's nvm on a glibc image and links none into
    /usr/local/bin: the plan loads nvm, and so does a mounted codex_bin's (its program and folder the mount's)."""
    installed = plan_of(launch.render("codex", settings("codex"), deploy=None, tools_url=None, job=tmp_path / "i"))
    assert installed["nvm"] is True and installed["command"][0] == "codex" and installed["path_prepend"] == []
    mounted = plan_of(launch.render("codex", settings("codex"), deploy=None, tools_url=None, job=tmp_path / "m",
                                    bin_path="/opt/hermit-codex/bin/codex", path_prepend=["/opt/hermit-codex/bin"]))
    assert mounted["nvm"] is True and mounted["command"][0] == "/opt/hermit-codex/bin/codex"
    assert mounted["path_prepend"] == ["/opt/hermit-codex/bin"]


def test_mini_swe_agent_runs_hermits_runner_from_its_bundle(tmp_path):
    plan = plan_of(launch.render("mini-swe-agent", settings("mini-swe-agent"), deploy=None, tools_url=None,
                                 job=tmp_path / "job"))
    assert plan["command"][:4] == ["python3", "-P", "-m", "hermit.agent.base.mini_swe_agent_run"]
    assert plan["bundle"]["container_dir"] == launch.BUNDLE_DIR and plan["env"]["PYTHONPATH"] == launch.BUNDLE_DIR
    bundle = Path(plan["bundle"]["host_dir"])
    files = sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file())
    assert files == sorted(launch.MINI_BUNDLE)
    # The runner imports nothing of Hermit the bundle lacks; the rest is the standard library, PyYAML, mini-swe-agent
    bundled = {".".join(Path(rel).with_suffix("").parts).removesuffix(".__init__")
               for rel in launch.MINI_BUNDLE if rel.endswith(".py")}
    allowed = {*sys.stdlib_module_names, "yaml", "minisweagent", "litellm"}
    for rel in (r for r in launch.MINI_BUNDLE if r.endswith(".py")):
        tree = ast.parse((bundle / rel).read_text())
        lazy = {id(n) for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == "__getattr__"
                for n in ast.walk(f)}                       # a package's attribute hook (hermit.Session): not run
        for node in ast.walk(tree):
            if id(node) in lazy:
                continue
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                assert (name in bundled) if name.startswith("hermit") else (name.split(".")[0] in allowed), (rel, name)


# ---- what never reaches the container ----------------------------------------------------------------------------

def test_a_secret_reaches_the_container_by_its_name_only(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN", "svc-token-0123456789abcdef")
    plan = plan_of(launch.render("codex", settings("codex", extra={"env": {"SERVICE_TOKEN": "$SERVICE_TOKEN",
                                                                           "PLAIN": "$HOME_LIKE", "N": "8"}}),
                                 deploy=None, tools_url=None, job=tmp_path / "job"))
    assert plan["env_from_host"]["SERVICE_TOKEN"] == "SERVICE_TOKEN" and plan["env"]["N"] == "8"
    assert "svc-token-0123456789abcdef" not in launch_texts(tmp_path / "job")
    with pytest.raises(ValueError, match="only whole"):            # a secret inside other text would be written out
        launch.render("codex", settings("codex", extra={"env": {"HEADER": "Bearer $SERVICE_TOKEN"}}), deploy=None,
                      tools_url=None, job=tmp_path / "bad")
    # A setting that writes a value or a path of this machine where the container reads it: the plan is refused
    with pytest.raises(RuntimeError, match="value of SERVICE_TOKEN"):
        launch.render("codex", settings("codex", extra={"args": ["-c", "token=svc-token-0123456789abcdef"]}),
                      deploy=None, tools_url=None, job=tmp_path / "leak")
    with pytest.raises(RuntimeError, match="path of this machine"):
        launch.render("codex", settings("codex", extra={"args": ["-c", f"x={tmp_path / 'path' / 'launch' / 'x'}"]}),
                      deploy=None, tools_url=None, job=tmp_path / "path")


def test_a_chatgpt_sign_in_is_a_secret_file_that_comes_back(tmp_path):
    from hermit.agent.base import login
    user = Path(os.environ["CODEX_HOME"]) / "auth.json"                   # the test's own (tests/conftest.py)
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text(json.dumps({"last_refresh": "2026-01-01T00:00:00Z", "tokens": {"refresh_token": "rt-stand-in"}}))
    job = tmp_path / "job"
    plan = plan_of(launch.render("codex", settings("codex", login="chatgpt"), deploy=None, tools_url=None, job=job,
                                 login="chatgpt"))
    home = Path(plan["home"]["host_dir"])
    assert plan["home"]["secret_files"] == ["auth.json"] and "rt-stand-in" in (home / "auth.json").read_text()
    assert plan["return_files"] == [{"from": f"{clis.CONTAINER_HOME}/auth.json",
                                     "to": str((job / "launch" / "return" / "auth.json").resolve())}]
    assert "rt-stand-in" not in (job / "launch" / "plan.json").read_text()
    assert login.master(login.ROOT).is_file()                            # the shared copy the home got
    # without the sign-in (a check, a digest): no secret file at all
    dry = plan_of(launch.render("codex", settings("codex", login="chatgpt"), deploy=None, tools_url=None,
                                job=tmp_path / "dry", login="chatgpt", sign_in=False))
    assert dry["home"]["secret_files"] == [] and not (tmp_path / "dry" / "launch" / "home" / "auth.json").exists()
    # a plan refused after the sign-in was copied leaves no copy of it
    with pytest.raises(RuntimeError):
        launch.render("codex", settings("codex", login="chatgpt", extra={"args": [str(tmp_path / "bad" / "x")]}),
                      deploy=None, tools_url=None, job=tmp_path / "bad", login="chatgpt")
    assert not (tmp_path / "bad" / "launch" / "home" / "auth.json").exists()


def test_a_claude_subscription_token_goes_on_a_descriptor(tmp_path, monkeypatch):
    from hermit.agent.base import cli
    with pytest.raises(ValueError, match="setup-token"):
        launch.render("claude", launch.settings_for("claude", model="claude-haiku-5-5", login="claude",
                                                    web_search=False),
                      deploy=None, tools_url=None, job=tmp_path / "none", login="claude")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    job = tmp_path / "job"
    plan = plan_of(launch.render("claude", launch.settings_for("claude", model="claude-haiku-5-5", login="claude",
                                                               web_search=False, effort="medium"),
                                 deploy=None, tools_url=None, job=job, login="claude"))
    assert plan["env_by_fd"] == {"CLAUDE_CODE_OAUTH_TOKEN": cli.CLAUDE_TOKEN_FD}
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in plan["env"] and plan["env_from_host"] == {}
    assert TOKEN not in launch_texts(job) and plan["command"][-2:] == ["--settings", cli.CLAUDE_LOCK]
    assert "--model" in plan["command"] and "claude-haiku-5-5" in plan["command"]


def test_the_memory_note_goes_with_the_prompt_and_the_notes_come_back(tmp_path):
    from hermit.agent.acting.workspace import MEMORY_NOTE
    job = tmp_path / "job"
    plan = plan_of(launch.render("codex", settings("codex"), deploy=staged(job, memory=True), tools_url=None, job=job))
    assert plan["prompt_suffix"] == "\n" + MEMORY_NOTE.format(path="hermit/demo/memory/NOTES.md")
    assert plan["deploy"] == {"work_dir": str((job / "deploy").resolve()), "adapter_id": "demo",
                              "context_file": "AGENTS.md", "context_limit": 262144, "context_escaped": None,
                              "hooks": False, "copy_back": ["hermit/demo/memory"]}
    plain = plan_of(launch.render("codex", settings("codex"), deploy=staged(tmp_path / "p"), tools_url=None,
                                  job=tmp_path / "p"))
    assert plain["prompt_suffix"] == "" and plain["deploy"]["copy_back"] == []


def test_dshs_context_limit_counts_the_byte_dsh_adds_for_each_closing_tag(tmp_path):
    """dsh gives the model the project's instructions in a frame within its cap, each `</system-reminder>` of them
    escaped by one more byte (hermit.hosts.dsh counts it so): the limit a dsh deployment's context gets, with
    Hermit's text's tags, is a text dsh takes whole and not a byte more, and the plan names the tag, so the image's
    own file merged in the container is counted so too (harborside)."""
    from hermit.agent.base.cli import DSH_INSTRUCTION_BYTES, DSH_INSTRUCTION_FRAME
    job = tmp_path / "job"
    deploy = staged(job)
    ours = "# demo\n<system-reminder>a</system-reminder> and <system-reminder>b</system-reminder>\n"
    (Path(deploy["deploy"]) / "AGENTS.md").write_text(ours)
    given = plan_of(launch.render("dsh", settings("dsh"), deploy=deploy, tools_url=None, job=job))["deploy"]
    limit = given["context_limit"]
    assert given["context_escaped"] == "</system-reminder>"

    def fits(size: int) -> bool:          # hermit.hosts.dsh's rule, for a text of `size` bytes holding Hermit's tags
        return size + DSH_INSTRUCTION_FRAME + ours.count("</system-reminder>") <= DSH_INSTRUCTION_BYTES
    assert fits(limit) and not fits(limit + 1)
    assert launch.context_limit("dsh", {}) == DSH_INSTRUCTION_BYTES - DSH_INSTRUCTION_FRAME     # no tag: the frame


# ---- the answer agent --------------------------------------------------------------------------------------------

def test_the_answer_agents_plan_has_no_command(tmp_path):
    plan = plan_of(launch.render_answer(settings("answer", effort="low"), endpoint="http://127.0.0.1:5/" + "0" * 32,
                                        job=tmp_path / "job", system="# context\n",
                                        options={"answer_pattern": r"ANSWER: (\w+)", "answer_format": "{answer}\n",
                                                 "attachments": ["/app/*.png"], "wait_s": 5}))
    assert plan["command"] is None and plan["home"] is None and plan["stream"] == "answer.json"
    assert plan["answer"] == {"endpoint": "http://127.0.0.1:5/" + "0" * 32, "model": "glm-5", "system": "# context\n",
                              "instruction_file": None, "wait_s": 5.0, "attachments": ["/app/*.png"],
                              "answer_file": "/app/answer.txt", "answer_pattern": r"ANSWER: (\w+)",
                              "answer_format": "{answer}\n", "max_tokens": None, "effort": "low"}
    with pytest.raises(ValueError, match="answer_format"):
        launch.render_answer({}, endpoint="x", job=tmp_path / "b", options={"answer_format": "no slot"})
    with pytest.raises(ValueError, match="takes instruction_file"):
        launch.render_answer({}, endpoint="x", job=tmp_path / "b", options={"temperature": 0})
    with pytest.raises(ValueError, match="no CLI to launch"):
        launch.render("answer", {}, deploy=None, tools_url=None, job=tmp_path / "b")


def test_the_answer_agents_null_options_keep_their_meaning(tmp_path):
    """answer_file: null is an answer written nowhere (harborside's answer agent writes none), not the default file;
    wait_s: null is the default wait, as an option left out is."""
    def answer(**options) -> dict:
        return plan_of(launch.render_answer(settings("answer"), endpoint="http://127.0.0.1:5/" + "0" * 32,
                                            job=tmp_path / "job", options=options))["answer"]
    assert answer(answer_file=None)["answer_file"] is None
    assert answer(wait_s=None)["wait_s"] == 30.0
    assert (answer()["answer_file"], answer()["wait_s"]) == ("/app/answer.txt", 30.0)
    assert answer(answer_file="")["answer_file"] == "/app/answer.txt"           # an empty one is none given


# ---- the digest --------------------------------------------------------------------------------------------------

def test_the_digest_is_the_launch_not_the_trial(tmp_path):
    def digest(job, url=URL, **kwargs):
        return launch.digest(launch.render("codex", settings("codex", **kwargs), deploy=None, tools_url=url,
                                           job=tmp_path / job, tools_name="demo"))
    first = digest("a")
    # another job and another server's token: the same launch
    assert digest("b", url="http://172.17.0.1:9/" + "cd" * 16 + "/mcp") == first
    assert digest("c", effort="high") != first                          # another setting: another launch
    assert digest("d", url=None) != first                                # no tools: another launch
    user = Path(os.environ["CODEX_HOME"]) / "auth.json"
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text(json.dumps({"last_refresh": "2026-01-01T00:00:00Z", "tokens": {"refresh_token": "one"}}))
    signed = launch.digest(launch.render("codex", settings("codex", login="chatgpt"), deploy=None, tools_url=None,
                                         job=tmp_path / "e", login="chatgpt"))
    user.write_text(json.dumps({"last_refresh": "2026-01-02T00:00:00Z", "tokens": {"refresh_token": "two"}}))
    again = launch.digest(launch.render("codex", settings("codex", login="chatgpt"), deploy=None, tools_url=None,
                                        job=tmp_path / "f", login="chatgpt"))
    assert signed == again                                               # a sign-in's contents are no part of it
