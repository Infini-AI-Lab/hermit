"""Golden parity checks: the framework's mwm port renders the same texts as
native metaworld for the same world-model state.

Skipped when the native checkout (MW_METAWORLD, default ~/code/metaworld) is
absent. Two surfaces are compared:

1. The learner contract for the clbench venue: native `contract.render`
   versus the CONTRACT.md the framework lifecycle writes with the clbench
   deployment. The framework keeps the native THREE RULES paragraph and
   changes only what rules 2 and 3 name (its wrapper files and the protected
   serving code); that substitution is the only admitted difference,
   everything else must be byte-identical.
2. The host push package for an empty world model: native
   `WorldModelInterface.package` versus the framework adapter's `context()`
   with the clbench deployment resource. The venue transport sentence
   (section 5's last line) differs by design; sections 1–4 and the built-in
   wm documentation must be identical.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TASK = Path(__file__).resolve().parents[1]
FRAMEWORK = TASK.parents[1]
METAWORLD = Path(os.environ.get("MW_METAWORLD", str(Path.home() / "code/metaworld")))
sys.path.insert(0, str(FRAMEWORK))


def _native_contract() -> str:
    env = dict(os.environ, PYTHONPATH=str(METAWORLD))
    code = ("import os; os.environ.setdefault('MW_DOCKET','1'); from mwm import contract; "
            "print(contract.render('clbench', captest_cmd='test_python'), end='')")
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True).stdout


def _framework_contract(tmp: Path) -> str:
    from harnessadapter.evolution.definition import EvolverDefinition
    session = tmp / "session"
    for name in ("adapter", "evidence", "scratch", "control", "method", "environment", "history", "deployment"):
        (session / name).mkdir(parents=True)
    shutil.copytree(FRAMEWORK / "harnessadapter/architectures/mwm", session / "adapter", dirs_exist_ok=True)
    shutil.copytree(TASK / "deployments/clbench", session / "deployment", dirs_exist_ok=True)
    (session / "evidence/README.md").write_text("# evidence\n")
    EvolverDefinition("mwm").prepare_session(session / "adapter", session, block=1, tick=1)
    return (session / "method/CONTRACT.md").read_text()


RULE2_NATIVE = "    2. Do not write the raw record (evidence/), env/, .git/, or the\n       wrapper's files (CONTRACT.md, AGENTS.md, CLAUDE.md). The standard\n       layout for world-model artifacts is "
RULE2_HERE = "    2. Do not write the raw record (evidence/), .git/, or the wrapper's\n       files (CONTRACT.md, EVOLVER.md). The standard layout for\n       world-model artifacts is "
RULE3_NATIVE = '    3. Do not edit state.json.\n'
RULE3_HERE = '    3. Do not edit the code that serves the world model to hosts:\n       adapter.json, interface.py, mwm_core/.\n'


def _normalise(text: str) -> str:
    """The only admitted difference: what rules 2 and 3 of THREE RULES name
    (this transport's wrapper files and its protected serving code)."""
    text = text.replace(RULE2_NATIVE, RULE2_HERE).replace(RULE3_NATIVE, RULE3_HERE)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


@unittest.skipUnless((METAWORLD / "mwm/contract.py").is_file(), "native metaworld checkout not available")
class ContractParity(unittest.TestCase):
    def test_contract_matches_native(self):
        native = _normalise(_native_contract())
        with tempfile.TemporaryDirectory() as tmp:
            ours = _normalise(_framework_contract(Path(tmp)))
        if native != ours:
            import difflib
            diff = "\n".join(list(difflib.unified_diff(native.splitlines(), ours.splitlines(), "native", "framework", lineterm=""))[:80])
            self.fail("contract differs from native:\n" + diff)


@unittest.skipUnless((METAWORLD / "mwm/wm_iface.py").is_file(), "native metaworld checkout not available")
class PackageParity(unittest.TestCase):
    def test_push_package_matches_native(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wrepo = tmp / "wrepo"
            shutil.copytree(FRAMEWORK / "harnessadapter/architectures/mwm", wrepo)
            evidence = tmp / "evidence"
            evidence.mkdir()
            (evidence / "README.md").write_text("# evidence\n")
            env = dict(os.environ, PYTHONPATH=str(METAWORLD))
            code = ("import sys; from mwm import wm_iface; "
                    f"i=wm_iface.WorldModelInterface({str(wrepo)!r}, {str(evidence)!r}, delegation='none', code_channel=True); "
                    "print(i.package(wm_iface.transport_note()), end='')")
            native = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True).stdout
            from harnessadapter.adapter.loader import Adapter
            from harnessadapter.adapter.resources import LearningMaterial
            host = json.loads((TASK / "deployments/clbench/host.json").read_text())
            ours = Adapter(wrepo, {"evidence": LearningMaterial(evidence), "deployment": host}).context()
        # The whole package: the deployment supplies the transport sentence, the
        # budget sentence, the library list and the native read behaviour.
        n, o = native, ours
        if n != o:
            import difflib
            diff = "\n".join(list(difflib.unified_diff(n.splitlines(), o.splitlines(), "native", "framework", lineterm=""))[:60])
            self.fail("push package differs from native:\n" + diff)


@unittest.skipUnless((METAWORLD / "mwm/session.py").is_file(), "native metaworld checkout not available")
class SessionPromptParity(unittest.TestCase):
    """The review-session prompt body (before the status block) equals native
    review_prompt() with the same block, evidence listing, notes and advice."""

    def test_review_prompt_matches_native(self):
        from harnessadapter.evolution.definition import EvolverDefinition
        evidence_listing = "### Hand 1 vs Tom\n- record: /evidence/block001/h001.json\n"
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "session"
            for name in ("adapter", "evidence", "scratch", "control", "method", "environment", "history", "deployment"):
                (session / name).mkdir(parents=True)
            shutil.copytree(FRAMEWORK / "harnessadapter/architectures/mwm", session / "adapter", dirs_exist_ok=True)
            shutil.copytree(TASK / "deployments/poker", session / "deployment", dirs_exist_ok=True)
            (session / "evidence/README.md").write_text("# evidence\n")
            d = EvolverDefinition("mwm")
            d.prepare_session(session / "adapter", session, block=3, tick=15)
            ours = d.build_prompt(session / "adapter", session / "evidence", evidence_listing, "EVOLVER.md", notes="parked: none")
        env = dict(os.environ, PYTHONPATH=str(METAWORLD))
        code = ("from mwm.session import review_prompt; from mwm import gates; "
                f"print(review_prompt(3, {evidence_listing!r}, notes='parked: none', advice=gates.render_advice([])), end='')")
        native = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True).stdout
        prefix = "Read ./CONTRACT.md first — it is the operating contract for this repository and you are judged against it.\n\n"
        self.assertTrue(ours.startswith(prefix), ours[:120])
        body = ours[len(prefix):].split("\n\n[Wrapper-computed status]")[0]
        self.assertEqual(native, body)


@unittest.skipUnless((METAWORLD / "mwm/env_surface.py").is_file(), "native metaworld checkout not available")
class EnvironmentReadmeParity(unittest.TestCase):
    """The deployment's environment README equals the native surface text; the
    only allowed difference is the invocation of the query channel (broker
    tool instead of the container client)."""

    def test_readmes_match_native(self):
        env = dict(os.environ, PYTHONPATH=str(METAWORLD), MW_META_SQL="1")
        for domain in ("clbench", "poker"):
            code = f"from mwm import env_surface; print(env_surface.readme({domain!r}), end='')"
            native = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True).stdout
            native = native.replace("/environment/bin/task-env query '<one SELECT statement>'", "the sql tool, sql(query='<one SELECT statement>')")
            ours = (TASK / "deployments" / domain / "environment/README.md").read_text()
            self.assertEqual(native.strip(), ours.strip(), domain)


@unittest.skipUnless((METAWORLD / "mwm/sandbox.py").is_file(), "native metaworld checkout not available")
class CodeChannelParity(unittest.TestCase):
    """wm run executes in the native sandbox, byte for byte: the same child
    program, builtins whitelist, preloaded libraries, caps and result shape."""

    def test_sandbox_module_is_native(self):
        native = (METAWORLD / "mwm/sandbox.py").read_bytes()
        ours = (FRAMEWORK / "harnessadapter/architectures/mwm/mwm_core/sandbox.py").read_bytes()
        self.assertEqual(native, ours)

    def test_run_result_shape(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("_sb", FRAMEWORK / "harnessadapter/architectures/mwm/mwm_core/sandbox.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not shutil.which("bwrap"):
            self.skipTest("bwrap not installed")
        out = module.run_sandboxed("import math\nresult = {'x': math.floor(args['v'])}", {"v": 2.5})
        self.assertEqual(out, {"success": True, "result": {"x": 2}})
        self.assertFalse(module.run_sandboxed("import os", {})["success"])


@unittest.skipUnless((METAWORLD / "mwm/wm_iface.py").is_file(), "native metaworld checkout not available")
class ToolSpecParity(unittest.TestCase):
    """The wm tool the host sees carries the native description and schema."""

    def test_wm_tool_matches_native(self):
        env = dict(os.environ, PYTHONPATH=str(METAWORLD))
        code = "import json; from mwm import wm_iface; print(json.dumps(wm_iface.TOOL_SPEC['function']))"
        native = json.loads(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True).stdout)
        tool = json.loads((FRAMEWORK / "harnessadapter/architectures/mwm/adapter.json").read_text())["tools"][0]
        self.assertEqual(tool["name"], native["name"])
        self.assertEqual(tool["description"], native["description"])
        self.assertEqual(tool["input_schema"]["properties"], native["parameters"]["properties"])
        self.assertEqual(tool["input_schema"]["required"], native["parameters"]["required"])


class ProtectedCore(unittest.TestCase):
    """The mwm architecture's core code is immutable for the learner: the
    broker refuses writes under it, validate() sees a broken context export,
    and the session records protected changes as a failed invariant."""

    def _session(self, tmp: Path) -> Path:
        from harnessadapter.evolution.definition import EvolverDefinition
        session = tmp / "session"
        for name in ("adapter", "evidence", "scratch", "control", "method", "environment", "history", "deployment"):
            (session / name).mkdir(parents=True)
        shutil.copytree(FRAMEWORK / "harnessadapter/architectures/mwm", session / "adapter", dirs_exist_ok=True)
        shutil.copytree(TASK / "deployments/poker", session / "deployment", dirs_exist_ok=True)
        (session / "evidence/README.md").write_text("# evidence\n")
        d = EvolverDefinition("mwm")
        d.prepare_session(session / "adapter", session, block=1, tick=1)
        (session / "control/protected.json").write_text(json.dumps(list(d.method.PROTECTED_PATHS)))
        return session

    def test_writes_refused_and_context_validated(self):
        from harnessadapter.evolution.workspace import Workspace, fingerprint
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(Path(tmp))
            ws = Workspace(session)
            for path in ("interface.py", "mwm_core/wm_iface.py", "adapter.json", "CONTRACT.md"):
                with self.assertRaises(ValueError, msg=path):
                    ws.write_file("adapter", path, "x = 1\n")
            ws.write_file("adapter", "memory/note.md", "a note\n")   # the world model itself stays writable
            before = fingerprint(session / "adapter")
            iface = session / "adapter/interface.py"
            iface.write_text(iface.read_text().replace("def context(", "def context_renamed(", 1))
            self.assertFalse(ws.validate()["passed"])
            self.assertNotEqual(ws.protected_files(before), ws.protected_files(fingerprint(session / "adapter")))


if __name__ == "__main__":
    unittest.main()
