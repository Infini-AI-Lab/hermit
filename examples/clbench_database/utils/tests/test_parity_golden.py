"""Golden parity: the framework's mwm (algorithms/templates/mwm + algorithms/evolvers/mwm) renders the same texts as native metaworld.

Ported from the old port's tasks/CLBench/tests/test_parity_golden.py to the current layout. Skipped when the native
checkout (MW_METAWORLD, default ~/code/metaworld) is absent. Surfaces compared:

1. the learner contract for the clbench venue (native contract.render vs the CONTRACT.md the Evolver writes); the
   only admitted difference is what rules 2 and 3 of THREE RULES name;
2. the host push package of an empty world model (native WorldModelInterface.package vs interface.context with the
   venue's host.json);
3. the review-session prompt body (native session.review_prompt vs prompt.build_prompt);
4. the venue environment READMEs (native env_surface.readme; the query channel is the sql tool here);
5. the wm run sandbox module, byte for byte, and its result shape;
6. the wm tool description and schema (native TOOL_SPEC vs exports.yaml).
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

TASKS = Path(__file__).resolve().parents[2]                 # examples/clbench_database/utils/agent
FRAMEWORK = TASKS.parents[1]
TEMPLATE = FRAMEWORK / "hermit/algorithms/templates/mwm"
EVOLVER = FRAMEWORK / "hermit/algorithms/evolvers/mwm"
METAWORLD = Path(os.environ.get("MW_METAWORLD", str(Path.home() / "code/metaworld")))
VENUES = {"clbench": TASKS, "poker": TASKS.parent / "clbench_poker"}
sys.path.insert(0, str(FRAMEWORK))
sys.path.insert(0, str(TASKS / "utils" / "agent"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PROMPT = _load("_mwm_parity_prompt", EVOLVER / "prompt.py")


def _native(code: str, **env) -> str:
    environ = dict(os.environ, PYTHONPATH=str(METAWORLD), **env)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=environ,
                          check=True).stdout


def _session(tmp: Path, domain: str, block: int, tick: int) -> tuple[Path, Path]:
    adapter = tmp / "adapter"
    shutil.copytree(TEMPLATE, adapter, ignore=shutil.ignore_patterns("__pycache__"))
    session = tmp / "session"
    (session / "control").mkdir(parents=True)
    shutil.copytree(VENUES[domain] / "utils/task-info/mwm", session / "deployment")
    PROMPT.prepare_session(adapter, session, block=block, tick=tick)
    return adapter, session


RULE2_NATIVE = ("    2. Do not write the raw record (evidence/), env/, .git/, or the\n       wrapper's files (CONTRACT.md, "
                "AGENTS.md, CLAUDE.md). The standard\n       layout for world-model artifacts is ")
RULE3_NATIVE = "    3. Do not edit state.json.\n"


def _normalise(text: str) -> str:
    """The only admitted difference: what rules 2 and 3 of THREE RULES name (this transport's wrapper files and
    its fixed serving code), taken from the Evolver's own substitution."""
    source = (EVOLVER / "prompt.py").read_text()
    rule2_here = re.search(r"rule2_here=(\".*?\")\n", source).group(1)
    rule3_here = re.search(r"rule3_here=('.*?')\n", source).group(1)
    text = text.replace(RULE2_NATIVE, eval(rule2_here)).replace(RULE3_NATIVE, eval(rule3_here))
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def _diff(a: str, b: str, n: int = 80) -> str:
    import difflib
    return "\n".join(list(difflib.unified_diff(a.splitlines(), b.splitlines(), "native", "framework",
                                               lineterm=""))[:n])


@unittest.skipUnless((METAWORLD / "mwm/contract.py").is_file(), "native metaworld checkout not available")
class ContractParity(unittest.TestCase):
    def test_contract_matches_native(self):
        native = _normalise(_native("import os; os.environ.setdefault('MW_DOCKET','1'); from mwm import contract; "
                                    "print(contract.render('clbench', captest_cmd='test_python'), end='')"))
        with tempfile.TemporaryDirectory() as tmp:
            adapter, session = _session(Path(tmp), "clbench", 1, 1)
            ours = _normalise((adapter / "CONTRACT.md").read_text())
            self.assertEqual((session / "method/CONTRACT.md").read_text(), (adapter / "CONTRACT.md").read_text())
        self.assertEqual(native, ours, "contract differs from native:\n" + _diff(native, ours))


@unittest.skipUnless((METAWORLD / "mwm/wm_iface.py").is_file(), "native metaworld checkout not available")
class PackageParity(unittest.TestCase):
    def test_push_package_matches_native(self):
        from hermit import Session
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wrepo = tmp / "wrepo"
            shutil.copytree(TEMPLATE, wrepo, ignore=shutil.ignore_patterns("__pycache__"))
            evidence = tmp / "evidence"
            evidence.mkdir()
            (evidence / "README.md").write_text("# evidence\n")
            native = _native("from mwm import wm_iface; "
                             f"i=wm_iface.WorldModelInterface({str(wrepo)!r}, {str(evidence)!r}, delegation='none', "
                             "code_channel=True); print(i.package(wm_iface.transport_note()), end='')")
            from hermit.algorithms.evolvers.mwm import deployment as _dep
            host = _dep.host_settings(TASKS / "utils/task-info/mwm")
            # The pipeline reads ADAPTER.md as the Evolver wrote it after an adopted step; write it the same way
            # first (interface.context with the venue's host.json), since no step has run on this copy.
            from hermit.adapter.loader import Adapter
            loaded = Session(wrepo, evidence=evidence, deployment=host).adapter
            (wrepo / "ADAPTER.md").write_text(loaded._module("interface.py").context(_resources=loaded.resources))
            ours = Session(wrepo, evidence=evidence, deployment=host).context()
        self.assertEqual(native, ours, "push package differs from native:\n" + _diff(native, ours, 60))


@unittest.skipUnless((METAWORLD / "mwm/session.py").is_file(), "native metaworld checkout not available")
class SessionPromptParity(unittest.TestCase):
    def test_review_prompt_matches_native(self):
        listing = "### Hand 1 vs Tom\n- record: /evidence/block001/h001.json\n"
        with tempfile.TemporaryDirectory() as tmp:
            adapter, session = _session(Path(tmp), "poker", 3, 15)
            ours = PROMPT.build_prompt(adapter, Path(tmp) / "evidence", listing, notes="parked: none", session=session)
        native = _native("from mwm.session import review_prompt; from mwm import gates; "
                         f"print(review_prompt(3, {listing!r}, notes='parked: none', "
                         "advice=gates.render_advice([])), end='')")
        prefix = ("Read ./CONTRACT.md first — it is the operating contract for this repository and you are judged "
                  "against it.\n\n")
        self.assertTrue(ours.startswith(prefix), ours[:120])
        self.assertEqual(native, ours[len(prefix):].split("\n\n[Wrapper-computed status]")[0])

    def test_bootstrap_prompt_matches_native(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, session = _session(Path(tmp), "clbench", 0, 0)
            ours = PROMPT.build_prompt(adapter, Path(tmp) / "evidence", "No episodes have been played yet.",
                                       session=session)
        # Native builds the text inline (mwm/session.py run_session); with the hypothesis mechanism off and the
        # docket on it is the concatenation of these literals, each of which must appear in the native source.
        fragments = [
            "BOOTSTRAP SESSION (before any episodes). Read CONTRACT.md fully, ",
            "then /environment/README.md. Where it lists public information, ",
            "read it and seed memory/ with what that information states, each ",
            "entry with its evidence extent (derived_from: the source path; ",
            "nothing graded yet) — none of it has been verified against the ",
            "environment, so say so in each entry's text",
            " — each entry naming the case shape that would test it, ",
            "with the stated value as the working default. ",
            "Where it lists none, memory/ starts empty. Build an initial ",
            "consumption/brief.md that honestly states what is unverified ",
            "— or that nothing is known yet",
            " — and record your open questions in meta/cases/."]
        source = (METAWORLD / "mwm/session.py").read_text()
        for fragment in fragments:
            self.assertIn(json.dumps(fragment, ensure_ascii=False), source)
        self.assertIn("".join(fragments), ours)


@unittest.skipUnless((METAWORLD / "mwm/env_surface.py").is_file(), "native metaworld checkout not available")
class EnvironmentReadmeParity(unittest.TestCase):
    def test_readmes_match_native(self):
        for domain in ("clbench", "poker"):
            native = _native(f"from mwm import env_surface; print(env_surface.readme({domain!r}), end='')",
                             MW_META_SQL="1")
            ours = (VENUES[domain] / "utils/task-info/mwm/environment/README.md").read_text()
            self.assertEqual(native.strip(), ours.strip(), domain)


@unittest.skipUnless((METAWORLD / "mwm/sandbox.py").is_file(), "native metaworld checkout not available")
class CodeChannelParity(unittest.TestCase):
    def test_sandbox_module_is_native(self):
        self.assertEqual((METAWORLD / "mwm/sandbox.py").read_bytes(),
                         (TEMPLATE / "mwm_core/sandbox.py").read_bytes())

    def test_run_result_shape(self):
        if not shutil.which("bwrap"):
            self.skipTest("bwrap not installed")
        module = _load("_mwm_parity_sandbox", TEMPLATE / "mwm_core/sandbox.py")
        out = module.run_sandboxed("import math\nresult = {'x': math.floor(args['v'])}", {"v": 2.5})
        self.assertEqual(out, {"success": True, "result": {"x": 2}})
        self.assertFalse(module.run_sandboxed("import os", {})["success"])


@unittest.skipUnless((METAWORLD / "mwm/wm_iface.py").is_file(), "native metaworld checkout not available")
class ToolSpecParity(unittest.TestCase):
    def test_wm_tool_matches_native(self):
        native = json.loads(_native("import json; from mwm import wm_iface; "
                                    "print(json.dumps(wm_iface.TOOL_SPEC['function']))"))
        tool = yaml.safe_load((TEMPLATE / "exports.yaml").read_text())["tools"][0]
        self.assertEqual(tool["name"], native["name"])
        self.assertEqual(tool["description"], native["description"])
        self.assertEqual(tool["input_schema"]["properties"], native["parameters"]["properties"])
        self.assertEqual(tool["input_schema"]["required"], native["parameters"]["required"])


if __name__ == "__main__":
    unittest.main()
