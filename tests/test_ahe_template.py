"""The ahe template (the AHE harness, the same for every host that runs its hooks): the root passes check_spec and is
a built-in id, the template check rejects every way a tree can drift off the surface with an issue naming the file,
deploys a copy to every host with the framework's deployment, measures the context file each host reads and runs every
hook the deployment installed as the host runs it, the deployments connect the fixed hooks so that they run from the
adapter's copy through a shell, and the seed middleware hands the memory over at session start (no model, no Docker,
no network)."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

import pytest

from hermit.config import Config
from hermit.spec import check_spec, repair

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "hermit" / "adapter" / "templates" / "ahe"
SKILL = "---\nname: tables\ndescription: reading a table of a bulletin\n---\nRun `python3 .agents/skills/tables/scripts/sum.py`.\n"
AGENT = "---\nname: reader\ndescription: reads long files and summarises them\n---\n\nSummarise what you read.\n"
MEMORY_WITH_ENTRIES = ("# Long-term memory\n\n## Agent Added Memories\n\n- Exact contract surfaces matter more than "
                       "equivalent substitutes.\n- Freeze the published state once the final check passes.\n")
EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "PostToolBatch",
          "Stop", "PreModelRequest"]
BEFORE_TOOL = "def before_tool(inp: HookInput, state: dict) -> HookResult | None:\n    return None"


@pytest.fixture
def tmp_path():
    """A directory outside /tmp: the template check runs under bubblewrap with an empty tmpfs at /tmp."""
    base = Path.home() / ".cache" / "hermit-tests"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    if not os.environ.get("KEEP_TEST_DIRS"):
        shutil.rmtree(path, ignore_errors=True)


def seed(tmp: Path, name: str = "adapter") -> Path:
    """A copy of the template root with one skill (and its helper), one script and one sub-agent on the surface."""
    adapter = tmp / name
    shutil.copytree(TEMPLATE, adapter, ignore=shutil.ignore_patterns("__pycache__"))
    (adapter / ".agents" / "skills" / "tables" / "scripts").mkdir(parents=True)
    (adapter / ".agents" / "skills" / "tables" / "SKILL.md").write_text(SKILL)
    (adapter / ".agents" / "skills" / "tables" / "scripts" / "sum.py").write_text("print(1 + 1)\n")
    (adapter / "scripts" / "count.py").write_text("import sys\nprint(len(sys.argv))\n")
    (adapter / "agents" / "reader.md").write_text(AGENT)
    set_prompt(adapter, (adapter / "AGENTS.md").read_text() + "\n- To count arguments, run `python3 scripts/count.py`.\n")
    return adapter


def set_prompt(adapter: Path, text: str) -> None:
    for name in ("AGENTS.md", "ADAPTER.md"):
        (adapter / name).write_text(text)


def template_check():
    spec = importlib.util.spec_from_file_location("ahe_check", TEMPLATE / ".hermit" / "check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def problems(adapter: Path) -> list[tuple[str, str]]:
    return [(i["path"], i["problem"]) for i in template_check().check(str(adapter))]


def interface(monkeypatch):
    """The template's hermit_hooks.py under the name a middleware imports, for this test only."""
    spec = importlib.util.spec_from_file_location("hermit_hooks", TEMPLATE / "hermit_hooks.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "hermit_hooks", module)
    spec.loader.exec_module(module)
    return module


def seed_middleware(monkeypatch):
    """The seed middleware imported as the runner imports it: `hermit_hooks` importable first."""
    interface(monkeypatch)
    spec = importlib.util.spec_from_file_location("ahe_seed_middleware", TEMPLATE / "middleware.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def deployed_agents_md(adapter: Path, tmp: Path) -> int:
    """The size of the AGENTS.md the framework's Codex deployment writes for `adapter`, with no size limit."""
    from hermit.hosts.codex import prepare
    work = Path(tempfile.mkdtemp(dir=tmp))
    prepare(adapter, work / "w", sys.executable, context_limit=None)
    return len((work / "w" / "AGENTS.md").read_bytes())


def test_template_root_is_a_built_in_id_and_passes_the_check(tmp_path):
    from hermit.adapter.loader import Adapter
    assert Config({"template": "ahe@1.0"}, tmp_path).template_dir() == TEMPLATE
    assert Config({"template": "ahe"}, tmp_path).template_dir() == TEMPLATE
    assert (TEMPLATE / "AGENTS.md").read_text() == (TEMPLATE / "ADAPTER.md").read_text()
    assert (TEMPLATE / "MEMORY.md").read_text() == "# Long-term memory\n\n## Agent Added Memories\n"
    assert sorted(p.name for p in (TEMPLATE / "agents").iterdir()) == ["README.md"]
    assert Adapter(TEMPLATE).agent_definitions() == []                         # the placeholder is no sub-agent
    result = check_spec(TEMPLATE, TEMPLATE, deploy=True)
    assert result.passed, result.issues
    adapter = seed(tmp_path)
    result = check_spec(adapter, TEMPLATE, deploy=True)
    assert result.passed, result.issues


def test_seed_rules_are_the_paper_prompt():
    """The three rules of AHE's seed system prompt are in AGENTS.md verbatim and are what the check protects."""
    rules = template_check().SEED_RULES
    prompt = (TEMPLATE / "AGENTS.md").read_text()
    assert len(rules) == 3 and all(f"- {rule}\n" in prompt for rule in rules)
    assert "is_background" not in prompt and "run_shell_command" not in prompt
    assert not any(line.startswith(("Date:", "Username:", "Working Dir:")) for line in prompt.splitlines())


def test_the_check_imports_the_stdlib_and_the_framework_only_for_the_deployment_and_the_frontmatter():
    tree = ast.parse((TEMPLATE / ".hermit" / "check.py").read_text())

    def tops(nodes):
        out = set()
        for node in nodes:
            if isinstance(node, ast.Import):
                out |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                out.add((node.module or "").split(".")[0])
        return out
    assert tops(tree.body) <= set(sys.stdlib_module_names)                     # loads with the stdlib alone
    assert tops(ast.walk(tree)) - set(sys.stdlib_module_names) == {"hermit"}
    framework = {f.name: {n.module for n in ast.walk(f) if isinstance(n, ast.ImportFrom)} - set(sys.stdlib_module_names)
                 for f in tree.body if isinstance(f, ast.FunctionDef)}
    # check_spec's rule for a deployment's failure, and the framework's YAML frontmatter reader: nothing else
    assert {name: mods for name, mods in framework.items() if mods} == {"deploy": {"hermit.spec"},
                                                                         "frontmatter": {"hermit.spec"}}
    check = template_check()                                       # and each host's deployment, by name
    assert {m for m, _, _ in check.DEPLOYMENTS.values()} == {"hermit.hosts.codex", "hermit.hosts.claude_code"}
    assert set(check.DEPLOYMENTS) == set(check.HOSTS) == {"codex", "claude-code"}


def test_the_learner_facing_texts_name_the_deployment_as_it_is():
    """No /adapter, no framework module, the Evolver's name, and the hook logs where the runner writes them."""
    texts = {rel: (TEMPLATE / rel).read_text() for rel in (".hermit/EVOLVER.md", "README.md", "scripts/README.md",
                                                         ".agents/skills/README.md", "agents/README.md",
                                                         "middleware.py", "exports.yaml")}
    for rel, text in texts.items():
        assert "/adapter" not in text and "hermit.runtime" not in text and "agents/.keep" not in text, rel
    assert "`ahe_adapter` Evolver" in texts["README.md"]
    guide = " ".join(texts[".hermit/EVOLVER.md"].split())
    assert ".hermit-hooks/" in guide and "hook-calls.jsonl" in guide and "hook-state/<session_id>.json" in guide
    assert "empty in `session_start`" in guide and "hermit/ahe/" in guide
    assert "the text of `task.md` in the session's workspace (`inp.workspace`)" in guide and "(`_prompt`)" in guide


def rewrite(adapter: Path, rel: str, old: str, new: str) -> None:
    path = adapter / rel
    text = path.read_text()
    assert old in text, (rel, old)
    path.write_text(text.replace(old, new))


def hook_body(adapter: Path, body: str) -> None:
    """before_tool of the seed middleware with another body."""
    rewrite(adapter, "middleware.py", BEFORE_TOOL,
            "def before_tool(inp: HookInput, state: dict) -> HookResult | None:\n" + body)


BROKEN = {
    "deleted seed rule": (lambda a: rewrite(a, "AGENTS.md", "- Prefer short replies; use the tool for actions.\n", ""),
                          "AGENTS.md", "seed rule \"Prefer short replies; use the tool for actions.\""),
    "reworded seed rule": (lambda a: rewrite(a, "AGENTS.md", "poll their output", "check on them"),
                           "AGENTS.md", "seed rule \"Long-running processes"),
    "empty prompt": (lambda a: (a / "AGENTS.md").write_text("\n"), "AGENTS.md", "AGENTS.md is empty"),
    "oversize prompt": (lambda a: (a / "AGENTS.md").write_text((a / "AGENTS.md").read_text() + "x" * 32_000),
                        "AGENTS.md", "-byte cap on Codex: the deployment adds"),
    # Under the nominal 32,000 bytes, but 500 headings nested two levels down and the deployment's own text push the
    # deployed AGENTS.md past the 32,768 bytes Codex reads.
    "prompt over the deployed limit": (lambda a: (a / "AGENTS.md").write_text(
        (a / "AGENTS.md").read_text() + "".join(f"## rule {i}\n" for i in range(500)) + "x" * 25_000),
        "AGENTS.md", "-byte cap on Codex: the deployment adds"),
    "ADAPTER.md drift": (lambda a: (a / "ADAPTER.md").write_text((a / "AGENTS.md").read_text() + "\nA learned line.\n"),
                         "ADAPTER.md", "ADAPTER.md is not AGENTS.md"),
    "missing MEMORY.md": (lambda a: (a / "MEMORY.md").unlink(), "MEMORY.md", "MEMORY.md is missing"),
    "MEMORY.md without its header": (lambda a: (a / "MEMORY.md").write_text("## Notes\n- x\n"),
                                     "MEMORY.md", "must start with `# Long-term memory`"),
    "middleware importing subprocess": (lambda a: rewrite(a, "middleware.py", "from pathlib import Path\n",
                                                          "from pathlib import Path\nimport subprocess\n"),
                                        "middleware.py", "imports `subprocess`"),
    "middleware importing urllib": (lambda a: rewrite(a, "middleware.py", "from pathlib import Path\n",
                                                      "from pathlib import Path\nimport urllib.request\n"),
                                    "middleware.py", "imports `urllib.request`"),
    "middleware importing a third-party package": (lambda a: rewrite(a, "middleware.py", "from pathlib import Path\n",
                                                                     "from pathlib import Path\nimport yaml\n"),
                                                   "middleware.py", "imports `yaml`, which is not where the agent runs"),
    "middleware without the MEMORY.md reference": (
        lambda a: (a / "middleware.py").write_text(
            "def session_start(inp, state):\n    return None\n\ndef before_tool(inp, state):\n    return None\n\n"
            "def after_tool(inp, state):\n    return None\n\ndef stop(inp, state):\n    return None\n\n"
            "def before_model(inp, state):\n    return None\n"),
        "middleware.py", "did not hand that entry to the model"),
    "middleware naming MEMORY.md only in a comment": (
        lambda a: (a / "middleware.py").write_text(
            "# reads MEMORY.md\ndef session_start(inp, state):\n    return None\n\ndef before_tool(inp, state):\n"
            "    return None\n\ndef after_tool(inp, state):\n    return None\n\ndef stop(inp, state):\n    return None\n\n"
            "def before_model(inp, state):\n    return None\n"),
        "middleware.py", "did not hand that entry to the model"),
    "middleware importing through importlib": (lambda a: hook_body(a, "    import importlib\n"
                                                                     "    importlib.import_module('socket')\n    return None"),
                                               "middleware.py", "imports `importlib`"),
    "middleware importing through __import__": (lambda a: hook_body(a, "    __import__('subprocess')\n    return None"),
                                                 "middleware.py", "calls `__import__`"),
    "middleware shelling out": (lambda a: hook_body(a, "    import os\n    os.system('curl x')\n    return None"),
                                "middleware.py", "calls `os.system`"),
    "middleware spawning": (lambda a: hook_body(a, "    import os\n    os.execvp('sh', ['sh'])\n    return None"),
                            "middleware.py", "calls `os.execvp`"),
    "middleware missing a hook": (lambda a: rewrite(a, "middleware.py", "def before_model(inp: HookInput, state: dict) -> HookResult | None:\n    return None\n", ""),
                                  "middleware.py", "defines no `before_model`"),
    "three-parameter hook": (lambda a: rewrite(a, "middleware.py", "def before_tool(inp: HookInput, state: dict)",
                                               "def before_tool(inp: HookInput, state: dict, extra: int = 0)"),
                             "middleware.py", "hook `before_tool` takes 3 parameters"),
    "misspelled hook": (lambda a: rewrite(a, "middleware.py", "def stop(inp: HookInput, state: dict)",
                                          "def on_stop(inp: HookInput, state: dict)"),
                        "middleware.py", "top-level function `on_stop` is not a hook"),
    "middleware that does not compile": (lambda a: (a / "middleware.py").write_text("def session_start(inp, state:\n"),
                                         "middleware.py", "does not compile"),
    "missing middleware": (lambda a: (a / "middleware.py").unlink(), "middleware.py", "middleware.py is missing"),
    # The dry run through hook_runner.py: what only running the module shows.
    "middleware raising in a hook": (lambda a: hook_body(a, "    raise RuntimeError('kaboom')"),
                                     "middleware.py", "`before_tool` fails in a dry run through hook_runner.py "
                                                      "on Codex (PreToolUse of a synthetic session, the state "
                                                      "carried over from the events before it): RuntimeError: "
                                                      "kaboom"),
    "middleware failing at import": (lambda a: rewrite(a, "middleware.py", "MEMORY_FILE = \"MEMORY.md\"\n",
                                                       "MEMORY_FILE = \"MEMORY.md\"\nTABLE = {}['missing']\n"),
                                     "middleware.py", "`session_start` fails in a dry run through hook_runner.py "
                                                      "on Codex (SessionStart"),
    "middleware returning junk": (lambda a: hook_body(a, "    return 'deny'"),
                                  "middleware.py", "TypeError: middleware must return None, HookResult or a dict"),
    "middleware returning an unknown decision": (lambda a: hook_body(a, "    return {'decision': 'maybe'}"),
                                                 "middleware.py", "ValueError: before_tool decision must be deny"),
    "middleware denying without a reason": (lambda a: hook_body(a, "    return {'decision': 'deny'}"),
                                            "middleware.py", "needs a non-empty reason"),
    "off-surface file": (lambda a: (a / "notes.txt").write_text("stray\n"), "notes.txt", "off the AHE harness surface"),
    "off-surface nested file": (lambda a: ((a / "sub_agents" / "x").mkdir(parents=True),
                                           (a / "sub_agents" / "x" / "AGENT.md").write_text("---\ndescription: d\n---\n")),
                                "sub_agents/x/AGENT.md", "off the AHE harness surface"),
    "skill without description": (lambda a: (a / ".agents" / "skills" / "tables" / "SKILL.md").write_text(
        "---\nname: tables\n---\nRun `python3 .agents/skills/tables/scripts/sum.py`.\n"),
        ".agents/skills/tables/SKILL.md", "needs a `description:`"),
    "skill whose description is a YAML comment": (lambda a: rewrite(a, ".agents/skills/tables/SKILL.md",
                                                                    "description: reading", "description: # reading"),
                                                  ".agents/skills/tables/SKILL.md", "needs a `description:`"),
    "skill whose frontmatter never closes on a whole `---` line": (
        lambda a: (a / ".agents" / "skills" / "tables" / "SKILL.md").write_text(
            "---\nname: tables\ndescription: reading a table\n---oops\nRun the table helper.\n"),
        ".agents/skills/tables/SKILL.md", "needs a `description:`"),
    "skill whose frontmatter holds a `---oops` line before its closing fence": (
        lambda a: (a / ".agents" / "skills" / "tables" / "SKILL.md").write_text(
            "---\nname: tables\ndescription: reading a table\n---oops\n---\nRun the table helper.\n"),
        ".agents/skills/tables/SKILL.md", "needs a `description:`"),
    "skill whose description is a YAML list": (lambda a: rewrite(a, ".agents/skills/tables/SKILL.md",
                                                                "description: reading a table of a bulletin",
                                                                "description: [reading, tables]"),
                                              ".agents/skills/tables/SKILL.md", "a YAML list, not a string"),
    "skill whose name is a YAML number": (lambda a: rewrite(a, ".agents/skills/tables/SKILL.md", "name: tables",
                                                            "name: 42"),
                                          ".agents/skills/tables/SKILL.md", "`name:` is a YAML int, not a string"),
    "agent whose description is a YAML list": (lambda a: rewrite(a, "agents/reader.md",
                                                                 "description: reads long files and summarises them",
                                                                 "description: [reads, summarises]"),
                                               "agents/reader.md", "`description:` a YAML list, not a string"),
    "skill named after another directory": (lambda a: rewrite(a, ".agents/skills/tables/SKILL.md", "name: tables",
                                                              "name: tabular"),
                                            ".agents/skills/tables/SKILL.md", "`name: tabular` OVERRIDES the directory name"),
    "agent without frontmatter": (lambda a: (a / "agents" / "reader.md").write_text("Summarise what you read.\n"),
                                  "agents/reader.md", "has no YAML frontmatter"),
    "agent without a description": (lambda a: (a / "agents" / "reader.md").write_text(
        "---\nname: reader\n---\nSummarise.\n"), "agents/reader.md", "needs both `name:` and `description:`"),
    "agent named unlike its file": (lambda a: rewrite(a, "agents/reader.md", "name: reader", "name: summariser"),
                                    "agents/reader.md", "differs from the file stem `reader`"),
    # Codex requires [a-z][a-z0-9_]{0,63} after `-` -> `_`: a digit first fails prepare() at the next rollout
    "agent named unlike a Codex identifier": (lambda a: (a / "agents" / "1st_reviewer.md").write_text(
        AGENT.replace("name: reader", "name: 1st_reviewer")), "agents/1st_reviewer.md", "must start with a letter"),
    # ... and two names one `-` -> `_` apart are one role there
    "agents colliding once installed": (lambda a: [(a / "agents" / f"{n}.md").write_text(AGENT.replace("name: reader",
                                                                                                    f"name: {n}"))
                                                   for n in ("code-checker", "code_checker")],
                                        "agents/code_checker.md", "install the role as `code_checker` (Codex)"),
    "SKILL.md referencing a missing script": (lambda a: (a / ".agents" / "skills" / "tables" / "scripts" / "sum.py").unlink(),
                                              ".agents/skills/tables/SKILL.md",
                                              "invokes `.agents/skills/tables/scripts/sum.py` but that file is not"),
    "AGENTS.md referencing a missing script": (lambda a: (a / "scripts" / "count.py").unlink(),
                                               "AGENTS.md", "invokes `scripts/count.py` but that file is not"),
    "AGENTS.md referencing a missing script by the relative form": (
        lambda a: rewrite(a, "AGENTS.md", "python3 scripts/count.py", "python3 ./scripts/other.py"),
        "AGENTS.md", "invokes `scripts/other.py` but that file is not"),
    "AGENTS.md referencing a missing script through the adapter's folder": (
        lambda a: rewrite(a, "AGENTS.md", "python3 scripts/count.py", "python3 hermit/ahe/scripts/other.py"),
        "AGENTS.md", "invokes `scripts/other.py` but that file is not"),
    "AGENTS.md referencing /adapter": (
        lambda a: rewrite(a, "AGENTS.md", "python3 scripts/count.py", "python3 /adapter/scripts/count.py"),
        "AGENTS.md", "invokes `/adapter/scripts/count.py`, but no deployment has /adapter"),
    "session_start naming MEMORY.md in its docstring only": (
        lambda a: (a / "middleware.py").write_text(
            'def session_start(inp, state):\n    """Hands MEMORY.md to the model."""\n    return None\n\n'
            "def before_tool(inp, state):\n    return None\n\ndef after_tool(inp, state):\n    return None\n\n"
            "def stop(inp, state):\n    return None\n\ndef before_model(inp, state):\n    return None\n"),
        "middleware.py", "did not hand that entry to the model"),
    "session_start naming MEMORY.md but handing nothing over": (
        lambda a: rewrite(a, "middleware.py", "    body = _memory_body(inp.adapter_dir)\n",
                          "    _unused = MEMORY_FILE\n    return None\n    body = _memory_body(inp.adapter_dir)\n"),
        "middleware.py", "did not hand that entry to the model"),
    "session_start reading MEMORY.md but handing over the task": (
        lambda a: rewrite(a, "middleware.py", "    return HookResult(context=body)\n",
                          "    return HookResult(context=inp.task_text)\n"),
        "middleware.py", "did not hand that entry to the model"),
    "script that does not compile": (lambda a: (a / "scripts" / "count.py").write_text("print(\n"),
                                     "scripts/count.py", "does not compile"),
}


@pytest.mark.parametrize("case", sorted(BROKEN))
def test_check_rejects_a_broken_tree_naming_the_file(tmp_path, case):
    break_it, path, expected = BROKEN[case]
    adapter = seed(tmp_path)
    assert problems(adapter) == []
    break_it(adapter)
    found = problems(adapter)
    assert any(p == path and expected in text for p, text in found), (case, found)


def test_the_prompt_cap_is_what_the_framework_deploys(tmp_path):
    """The cap is measured on a deployment: AGENTS.md exactly at the cap passes, one byte more fails, and the issue
    names the bytes a deployment of that adapter really has."""
    adapter = seed(tmp_path)
    base = (adapter / "AGENTS.md").read_text() + "".join(f"## rule {i}\n" for i in range(50))
    set_prompt(adapter, base)
    overhead = deployed_agents_md(adapter, tmp_path) - len(base.encode())
    cap = min(32_000, 32_768 - overhead)
    assert 1_000 < overhead < 3_000 and cap < 32_000                # the seed's deployment, not a constant
    set_prompt(adapter, base + "x" * (cap - len(base.encode()) - 1) + "\n")
    assert len((adapter / "AGENTS.md").read_bytes()) == cap and problems(adapter) == []
    assert check_spec(adapter, TEMPLATE, deploy=True).passed
    set_prompt(adapter, base + "x" * (cap - len(base.encode())) + "\n")
    found = [text for path, text in problems(adapter) if path == "AGENTS.md"]
    assert len(found) == 1, found
    size, said_cap, added, deployed = map(int, re.search(
        r"is (\d+) bytes, over the (\d+)-byte cap on Codex: the deployment adds (\d+) bytes .* making (\d+) bytes",
        found[0]).groups())
    assert (size, said_cap, added) == (cap + 1, cap, overhead) and deployed == deployed_agents_md(adapter, tmp_path)
    from hermit.hosts.codex import prepare
    with pytest.raises(ValueError, match="past the 32768 bytes"):                # what the check spared the rollout
        prepare(adapter, tmp_path / "refused", sys.executable)


def test_a_deploy_failure_names_the_file_its_message_names(tmp_path, monkeypatch):
    """A deployment that refuses the adapter: the issue names the file the refusal names, found as check_spec finds
    it (hermit.spec._path_in) once the scratch copy's path is taken off, so that repair reverts that file alone; a
    refusal that names no file names none."""
    import hermit.hosts.codex as codex
    adapter = seed(tmp_path)

    def refusing(text):
        def prepare(copy, workspace, python, **_):
            raise ValueError(text.format(copy=copy))
        return prepare
    for text, said, path in (("cannot install the role {copy}/agents/reader.md",
                              "cannot install the role agents/reader.md", "agents/reader.md"),
                             ("Agent names must be unique, lowercase Codex identifiers",
                              "Agent names must be unique, lowercase Codex identifiers", "")):
        monkeypatch.setattr(codex, "prepare", refusing(text))
        found = [(p, t) for p, t in problems(adapter) if "does not deploy to Codex" in t]
        assert found == [(path, "the adapter does not deploy to Codex, so AGENTS.md cannot be measured as the agent "
                                f"reads it: ValueError: {said}")], found


def test_check_passes_a_grown_tree(tmp_path):
    """A learned middleware with private helpers, a class, a dict return and prints, a longer prompt, memory entries,
    and scripts named as a deployment runs them."""
    adapter = seed(tmp_path)
    (adapter / "middleware.py").write_text(
        "import json\nimport re\nfrom hermit_hooks import HookResult\n\nRX = re.compile(r'rm -rf /')\n"
        "print('imported')\n\n"
        "class _Guard:\n    def hit(self, cmd):\n        return bool(RX.search(cmd or ''))\n\n"
        "def _memory(d):\n    return open(d + '/MEMORY.md').read()\n\n"
        "def session_start(inp, state):\n    print('a print never reaches the wire')\n"
        "    return HookResult(context=_memory(inp.adapter_dir))\n\n"
        "def before_tool(inp, state):\n    if inp.tool_kind == 'shell' and _Guard().hit(inp.command):\n"
        "        return {'decision': 'deny', 'reason': 'not in this harness'}\n    return None\n\n"
        "def after_tool(inp, state):\n    state['calls'] = state.get('calls', 0) + 1\n    return None\n\n"
        "def stop(inp, state):\n    return None\n\n"
        "def before_model(inp, state):\n    inp.queue_context(json.dumps(state))\n    return None\n")
    (adapter / "MEMORY.md").write_text(MEMORY_WITH_ENTRIES)
    (adapter / "scripts" / "run-tests.py").write_text("print('tests')\n")     # the manifest's scripts/*.py, any name
    (adapter / "scripts" / "Fetch.py").write_text("print('fetch')\n")
    set_prompt(adapter, (adapter / "AGENTS.md").read_text() + "- Tests: `cd hermit/ahe && python3 "
               "scripts/run-tests.py`, or `python3 hermit/ahe/scripts/Fetch.py` from the workspace.\n")
    (adapter / "agents" / "code-checker.md").write_text(AGENT.replace("name: reader", "name: code-checker"))
    assert problems(adapter) == []
    result = check_spec(adapter, TEMPLATE, deploy=True)
    assert result.passed, result.issues
    assert "hermit.runtime.hooks" in template_check().HOOK_MODULES             # trained adapters' fallback import
    rewrite(adapter, "middleware.py", "from hermit_hooks import HookResult\n",
            "try:\n    from hermit_hooks import HookResult\nexcept ImportError:\n"
            "    from hermit.runtime.hooks import HookResult\n")
    assert problems(adapter) == []


def test_a_middleware_that_selects_the_memory_by_the_task_passes_the_memory_run(tmp_path):
    """The memory run's session has a task on the probe entry's topic: a session_start that hands over only the
    entries sharing a word with the task keeps the channel and passes."""
    adapter = seed(tmp_path)
    rewrite(adapter, "middleware.py", "    return HookResult(context=body)\n",
            "    words = set(inp.task_text.lower().split())\n"
            "    kept = [l for l in body.splitlines() if l.startswith('- ') and words & set(l.lower().split())]\n"
            "    return HookResult(context='\\n'.join(kept)) if kept else None\n")
    assert problems(adapter) == []


def test_a_memory_path_built_by_a_call_passes(tmp_path):
    """The memory channel is what session_start hands over, not how the file is named: a module-level
    `MEMORY_FILE = Path("MEMORY.md")` keeps the channel and passes."""
    adapter = seed(tmp_path)
    rewrite(adapter, "middleware.py", 'MEMORY_FILE = "MEMORY.md"\n', 'MEMORY_FILE = Path("MEMORY.md")\n')
    assert problems(adapter) == []


def test_frontmatter_is_read_as_yaml(tmp_path):
    """A skill's or a sub-agent's frontmatter is YAML, as the hosts and the deployment read it: a `name:` followed by
    a comment is the name alone."""
    adapter = seed(tmp_path)
    rewrite(adapter, ".agents/skills/tables/SKILL.md", "name: tables", "name: tables  # the directory's name")
    rewrite(adapter, "agents/reader.md", "name: reader", "name: reader  # the file's stem")
    assert problems(adapter) == []


def test_check_spec_attributes_template_issues_to_their_files_and_repair_reverts_them(tmp_path):
    parent = seed(tmp_path, "parent")
    adapter = seed(tmp_path, "adapter")
    rewrite(adapter, "AGENTS.md", "- Prefer short replies; use the tool for actions.\n", "")
    (adapter / "ADAPTER.md").write_text((adapter / "AGENTS.md").read_text())
    (adapter / "MEMORY.md").write_text(MEMORY_WITH_ENTRIES)           # a valid change that must survive the repair
    (adapter / "notes.txt").write_text("stray\n")
    hook_body(adapter, "    raise RuntimeError('kaboom')")
    (adapter / "hook_runner.py").write_text("# tampered\n")          # a fixed file: back to the root's
    result = check_spec(adapter, TEMPLATE, deploy=True)
    checks = {(i.path, i.check) for i in result.issues}
    assert {("AGENTS.md", 6), ("notes.txt", 2), ("middleware.py", 6), ("hook_runner.py", 1)} <= checks, result.issues
    final, repaired = repair(adapter, TEMPLATE, parent)
    assert final.passed, final.issues
    assert {r["path"] for r in repaired} >= {"AGENTS.md", "notes.txt", "middleware.py", "hook_runner.py"}
    assert "Prefer short replies" in (adapter / "AGENTS.md").read_text()
    assert (adapter / "MEMORY.md").read_text() == MEMORY_WITH_ENTRIES
    assert (adapter / "hook_runner.py").read_text() == (TEMPLATE / "hook_runner.py").read_text()
    assert not (adapter / "notes.txt").exists()


def test_check_refuses_an_adapter_it_cannot_see(tmp_path):
    found = problems(tmp_path / "nowhere")
    assert found and found[0][0] == "AGENTS.md" and "not visible" in found[0][1]


def test_the_fixed_hooks_file_registers_the_runner_on_every_event_of_every_host():
    from hermit.adapter.loader import hook_definitions, load_manifest
    manifest = load_manifest(TEMPLATE)
    assert manifest["hooks"] == "hooks.json" and "middleware" not in manifest
    hooks = hook_definitions(TEMPLATE, manifest)["hooks"]
    assert list(hooks) == EVENTS
    spec = importlib.util.spec_from_file_location("ahe_runner_constants", TEMPLATE / "hook_runner.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    for event, groups in hooks.items():
        assert groups == [{"hooks": [{"type": "command", "command": f"python3 -B hook_runner.py {event}",
                                      "timeout": runner.TIMEOUT_S}]}]
    assert set(EVENTS) == {e for wire in runner.hosts.HOSTS.values() for e in wire.events}   # each host's, no more


def test_codex_deploy_writes_the_context_the_hooks_and_the_skills(tmp_path):
    """The framework's Codex deployment of the seed: the managed block, the fixed hooks run from the adapter's copy
    (`cd <copy> || exit 1` before the runner's command), the sample skill copied, the sub-agent as a role."""
    from hermit.hosts.codex import prepare
    adapter = seed(tmp_path)
    work = tmp_path / "work"
    result = prepare(adapter, work, sys.executable)
    copy = Path(result["adapter_dir"])
    assert copy == work.resolve() / "hermit" / "ahe"
    agents_md = (work / "AGENTS.md").read_text()
    assert "<!-- hermit:ahe:start -->" in agents_md and "Prefer short replies" in agents_md
    assert "This adapter's files are in `hermit/ahe/`" in agents_md
    assert "`hermit/ahe/hook_runner.py`: The hook runner a host starts on each lifecycle event" in agents_md
    hooks_path = work / ".codex" / "hooks.json"
    assert result["hooks"] == str(hooks_path) and result["placed"] == {"skills": "native", "agents": "native",
                                                                       "hooks": "native"}
    hooks = json.loads(hooks_path.read_text())["hooks"]
    assert list(hooks) == EVENTS
    for event in EVENTS:
        (group,) = hooks[event]
        assert group["hooks"] == [{"type": "command", "command": f"cd {shlex.quote(str(copy))} || exit 1\n"
                                                                 f"python3 -B hook_runner.py {event}", "timeout": 30}]
    for name in ("hook_runner.py", "hermit_hooks.py", "hook_hosts.py", "middleware.py", "MEMORY.md", "hooks.json"):
        assert (copy / name).read_bytes() == (adapter / name).read_bytes(), name
    assert result["skills"] == ["tables"] and (work / ".codex" / "skills" / "tables" / "scripts" / "sum.py").is_file()
    assert not (work / ".codex" / "skills" / "README.md").exists()      # the placeholder is not a skill
    assert set(result["roles"]) == {"reader"} and (work / ".codex" / "agents" / "reader.toml").is_file()
    assert "[hooks" not in (work / ".codex" / "config.toml").read_text()   # never a second hook source in one layer
    prepare(adapter, work, sys.executable)                                  # redeploy into the same workspace


DENY = ("    state['task_seen'] = inp.task_text\n"
        "    if inp.tool_kind == 'shell' and 'rm -rf' in (inp.command or ''):\n"
        "        return HookResult(decision='deny', reason='destructive: list with git clean -n first')\n"
        "    return None")


def test_a_deployed_hook_runs_from_the_copy_through_a_shell(tmp_path):
    """One PreToolUse as Codex 0.164 runs it (the deployed command through a shell, in the session's workspace):
    the deny on stdout, one audit line under the workspace's .hermit-hooks/, the session's state beside it, the task
    text from the session's UserPromptSubmit or, once the workspace has one, from its task.md (the payload's cwd, not
    the copy the command runs in), and no byte code written into the copy."""
    from hermit.hosts.codex import prepare
    adapter = seed(tmp_path)
    hook_body(adapter, DENY)
    rewrite(adapter, "middleware.py", "    body = _memory_body(inp.adapter_dir)\n",
            "    state['start_task'] = inp.task_text\n    body = _memory_body(inp.adapter_dir)\n")
    assert problems(adapter) == []
    work = tmp_path / "work"
    prepare(adapter, work, sys.executable)
    commands = {e: g[0]["hooks"][0]["command"] for e, g in json.loads((work / ".codex" / "hooks.json").read_text())
                ["hooks"].items()}

    def hook(event, **fields):
        data = {"hook_event_name": event, "session_id": "s-1", "cwd": str(work), "transcript_path": None,
                "model": "gpt-5.6-luna", **fields}
        env = {k: v for k, v in os.environ.items() if k != "HERMIT_HOOK_LOGS"}
        return subprocess.run(["sh", "-c", commands[event]], input=json.dumps(data), capture_output=True, text=True,
                              cwd=work, env=env)

    assert hook("SessionStart").stdout == ""
    assert hook("UserPromptSubmit", turn_id="t-1", prompt="Read task.md and answer.").stdout == ""
    proc = hook("PreToolUse", turn_id="t-1", tool_name="Bash", tool_input={"command": "rm -rf build"},
                tool_use_id="call-1")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "destructive: list with git clean -n first"}}
    lines = [json.loads(l) for l in (work / ".hermit-hooks" / "hook-calls.jsonl").read_text().splitlines()]
    assert [l["event"] for l in lines] == ["SessionStart", "UserPromptSubmit", "PreToolUse"]
    assert list(lines[-1]) == ["ts", "event", "session_id", "tool_name", "command", "decision", "reason",
                               "context_chars", "ms", "error"]                 # the audit line as it ran on Harbor
    assert (lines[-1]["decision"], lines[-1]["command"], lines[-1]["error"]) == ("deny", "rm -rf build", None)
    (state_file,) = (work / ".hermit-hooks" / "hook-state").iterdir()
    state = json.loads(state_file.read_text())
    assert state_file.name == "s-1.json" and state["start_task"] == "" and state["task_seen"] == "Read task.md and answer."
    assert state["_prompt"] == "Read task.md and answer."
    (work / "task.md").write_text("## Question\nWhat changed in FY 1934?\n")
    assert hook("SessionStart", session_id="s-2").returncode == 0
    state = json.loads((work / ".hermit-hooks" / "hook-state" / "s-2.json").read_text())
    assert state["start_task"] == "## Question\nWhat changed in FY 1934?\n" and "_prompt" not in state
    assert not list((work / "hermit").rglob("__pycache__"))


def test_seed_middleware_hands_the_memory_over_at_session_start(tmp_path, monkeypatch):
    middleware = seed_middleware(monkeypatch)
    hooks = sys.modules["hermit_hooks"]
    adapter = seed(tmp_path)
    inp = lambda event: hooks.HookInput(event=event, host="codex", session_id="s", cwd=str(tmp_path),
                                        adapter_dir=str(adapter))
    assert middleware.session_start(inp("session_start"), {}) is None           # header-only MEMORY.md
    (adapter / "MEMORY.md").write_text(MEMORY_WITH_ENTRIES)
    state = {}
    result = middleware.session_start(inp("session_start"), state)
    assert isinstance(result, hooks.HookResult) and result.decision is None
    assert result.context.startswith("## Agent Added Memories") and "Freeze the published state" in result.context
    assert "# Long-term memory" not in result.context and state["memory_chars"] == len(result.context)
    (adapter / "MEMORY.md").write_text("# Long-term memory\n\n## Agent Added Memories\n\n### Always run the suite\n")
    result = middleware.session_start(inp("session_start"), {})                 # an entry written as a heading counts
    assert result.context == "## Agent Added Memories\n\n### Always run the suite"
    (adapter / "MEMORY.md").write_text("# Long-term memory\n\n## Agent Added Memories\n\n")
    assert middleware.session_start(inp("session_start"), {}) is None           # the seed's section line alone: no entry
    (adapter / "MEMORY.md").unlink()
    assert middleware.session_start(inp("session_start"), {}) is None           # fail open: no file, no memory
    for event in ("before_tool", "after_tool", "stop", "before_model"):
        assert getattr(middleware, event)(inp(event), {}) is None


def test_seed_middleware_imports_without_the_interface(tmp_path):
    """A bare interpreter where `hermit_hooks` is not importable still loads the module and gets the memory back
    (the guarded import's fallback classes)."""
    adapter = seed(tmp_path)
    (adapter / "MEMORY.md").write_text(MEMORY_WITH_ENTRIES)
    script = (
        "import importlib.util, json, sys\n"
        "sys.modules['hermit_hooks'] = None\n"
        f"spec = importlib.util.spec_from_file_location('mw', {json.dumps(str(adapter / 'middleware.py'))})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"r = m.session_start(m.HookInput(event='session_start', adapter_dir={json.dumps(str(adapter))}), {{}})\n"
        "print(json.dumps({'module': m.HookResult.__module__, 'context': r.context}))\n")
    run = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, check=True)
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["module"] == "mw" and "Freeze the published state" in out["context"]
