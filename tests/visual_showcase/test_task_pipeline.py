"""pipeline.py: the three arms, budgets, image retention, the run log layout and the transport, with a scripted fake
ChatClient and a fake capture (ported from the old test_harness.py CampaignTests). No network, no Chrome."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import sys
import urllib.error

import pytest
import yaml

from harnessadapter.evolver.base import Usage
from harnessadapter.task import RunOutput

TASK = Path(__file__).resolve().parents[2] / "examples" / "tasks" / "visual_showcase"
sys.path.insert(0, str(TASK))
import harness  # noqa: E402
import pipeline  # noqa: E402
import prepare  # noqa: E402

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=")
HTML = "<!doctype html><html><body>ORIGINAL_MARK</body></html>"
SHOTS = ["desktop-t0", "desktop-t2", "desktop-t5", "desktop-interaction", "phone"]
PRIVATE = "SYNTHETIC_PRIVATE_FIELD"
KEY = "SYNTHETIC_TEST_KEY_123"
TASKS = {json.loads(l)["id"]: json.loads(l) for l in
         prepare.generated(prepare.check_inputs())["tasks.jsonl"].splitlines()}       # the rows prepare.py writes


def image_bytes(name: str, workspace: Path) -> bytes:
    """Distinct bytes per screenshot and per inspected file, so batches can be told apart."""
    return PNG + name.encode() + harness.sha(Path(workspace) / "index.html")[:8].encode()


def synthetic_capture(workspace, output, case, timeout=90):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    shots = []
    for name in SHOTS:
        data = image_bytes(name, workspace)
        (output / f"{name}.png").write_bytes(data)
        shots.append({"id": name, "label": f"Synthetic {name}", "file": f"{name}.png",
                      "sha256": hashlib.sha256(data).hexdigest(), "state": {"intensity": 0.75}})
    (output / "diagnostic-paused.png").write_bytes(PNG)
    summary = {"case": case, "capture_ok": True, "status": "synthetic_test_only", "screenshots": shots,
               "input": {"index_sha256": harness.sha(Path(workspace) / "index.html")}, "page_errors": [],
               "console_errors": [], "observations": {"ready_contract": True}}
    harness.save(output / "summary.json", summary)
    return summary


def reply(content="", calls=None, finish=None, tokens=100):
    message = {"role": "assistant", "content": content, "reasoning_content": PRIVATE}
    if calls:
        message["tool_calls"] = calls
    return message, tokens, finish or ("tool_calls" if calls else "stop")


def tool(call_id, name, args):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class FakeClient:
    """Scripted ChatClient: records each request; replies are (message, completion tokens, finish) or an exception."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.timeout = None
        self.total = Usage(pipeline.MODEL, cost_usd=0.0)
        self.last_meta = {}

    def chat(self, messages, **options):
        self.requests.append({"messages": copy.deepcopy(messages), "options": options, "timeout": self.timeout})
        item = self.replies[len(self.requests) - 1]
        if isinstance(item, Exception):
            raise item
        message, tokens, finish = item
        cost = (40 * 0.15 + tokens * 0.5) / 1e6
        t = self.total
        t.input_tokens += 40; t.output_tokens += tokens; t.calls += 1; t.cost_usd += cost
        self.last_meta = {"http_status": 200, "seconds": 0.01, "usage": {
            "prompt_tokens": 40, "completion_tokens": tokens, "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 4}}, "response_model": pipeline.MODEL,
            "response_id": "synthetic"}
        return {**copy.deepcopy(message), "finish_reason": finish}, Usage(pipeline.MODEL, 40, 0, tokens, 1, cost)


RUNTIME_VERSIONS = {"node": "20.0.0", "playwright": "1.62.1", "pngjs": "7.0.0", "chrome": "Synthetic Chrome 1.0"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VISUAL_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setenv("FIREWORKS_API_KEY", KEY)
    captures = []

    def fake_capture(*args, **kwargs):
        captures.append(args)
        return synthetic_capture(*args, **kwargs)
    monkeypatch.setattr(harness, "capture", fake_capture)
    monkeypatch.setattr(harness, "check_browser", lambda: dict(RUNTIME_VERSIONS))   # the fake capture needs no browser

    class Env:
        root = tmp_path
        client = None

        def run(self, replies, adapter=None, task="waves", direct=False):
            self.client = FakeClient(replies)
            monkeypatch.setattr(pipeline, "make_client", lambda: self.client)
            fn = pipeline.run_direct if direct else pipeline.run
            out = fn(dict(TASKS[task]), adapter)
            assert isinstance(out, RunOutput)
            self.out, self.log = out, Path(out.log)
            self.result = json.loads((self.log / "result.json").read_text())
            self.request = json.loads((self.log / "request.json").read_text())
            return self.result
    e = Env()
    e.captures = captures
    return e


def make_adapter(root: Path, origin: bool, context="SYNTHETIC KNOWLEDGE PACKAGE") -> Path:
    """A minimal adapter exporting wm and context_package like the W0 template; `origin` marks a learned one."""
    root.mkdir(parents=True)
    manifest = {"template": {"id": "fake-visual", "version": "1.0"},
                "exports": {"tools": [
                    {"name": "wm", "description": "Synthetic world model.", "entrypoint": "tools.py:wm",
                     "input_schema": {"type": "object", "properties": {"name": {"type": "string"},
                                                                       "args": {"type": "object"}},
                                      "required": ["name"], "additionalProperties": False},
                     "output_schema": {"type": "object"}},
                    {"name": "context_package", "description": "Synthetic package.",
                     "entrypoint": "tools.py:context_package",
                     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                     "output_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                                       "required": ["text"]}}]},
                "structure": {"learnable": [{"path": "memory/**/*.md", "format": {"text": True}}],
                              "fixed": ["tools.py"]}}
    if origin:
        manifest["origin"] = {"template": "fake-visual", "version": "1.0"}
    (root / "adapter.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    (root / "ADAPTER.md").write_text("Synthetic adapter.\n")
    (root / "tools.py").write_text(
        "def wm(name, args=None):\n    return {'ok': True, 'name': name, 'result': 'SYNTHETIC_MEMORY'}\n\n"
        f"def context_package():\n    return {{'text': {context!r}}}\n")
    return root


def all_text(log: Path) -> str:
    return "\n".join(p.read_text(errors="replace") for p in log.rglob("*") if p.is_file() and p.suffix != ".png")


# ---------- arm A ----------

def test_arm_a_one_request_no_tools_and_mechanical_extraction(env):
    result = env.run([reply("```html\n" + HTML + "\n```")], direct=True)
    assert len(env.client.requests) == 1
    request = env.client.requests[0]
    assert "tools" not in request["options"] and "tool_choice" not in request["options"]
    assert request["options"] == {**pipeline.SAMPLING, "max_tokens": 32768}
    assert request["messages"] == [{"role": "system", "content": pipeline.COMMON + "\n\n" + pipeline.DIRECT},
                                   {"role": "user", "content": TASKS["waves"]["prompt"]}]
    assert (result["arm"], result["status"], result["extraction"]) == ("direct", "completed", "single_html_fence")
    assert (result["model_calls"], result["tool_calls"], result["valid_submission"]) == (1, 0, True)
    assert (env.log / "submission" / "index.html").read_text() == HTML
    assert (env.log / "response.txt").read_text() == "```html\n" + HTML + "\n```"
    assert env.request["tools"] == [] and env.log.name.startswith("waves-direct-")
    assert env.request["capture_runtime"] == RUNTIME_VERSIONS      # what check_browser found, for the record
    assert [Path(c[1]).name for c in env.captures] == ["capture"]   # only the host's final capture


def test_arm_a_invalid_output_is_not_reconstructed(env):
    result = env.run([reply("I did not write an HTML document.")], direct=True)
    assert result["status"] == "invalid_output" and not result["valid_submission"]
    assert "No model artifact was submitted." in (env.log / "submission" / "index.html").read_text()


def test_arm_a_truncated_reply_and_adapter_ignored(env, tmp_path):
    adapter = make_adapter(tmp_path / "learned", origin=True)
    result = env.run([reply(HTML, finish="length")], adapter=adapter, direct=True)
    assert (result["arm"], result["status"]) == ("direct", "output_truncated")
    assert env.request["adapter"]["used"] is False and result["adapter_context_sha256"] is None
    assert env.client.requests[0]["messages"][0]["content"] == pipeline.COMMON + "\n\n" + pipeline.DIRECT


# ---------- arm selection ----------

def test_arm_selection_by_origin_key(env, tmp_path):
    root = make_adapter(tmp_path / "root", origin=False)
    learned = make_adapter(tmp_path / "learned", origin=True)
    assert (pipeline.arm_of(None), pipeline.arm_of(root), pipeline.arm_of(learned)) == ("harness", "harness", "adapted")
    for adapter in (None, root):
        result = env.run([reply("done")], adapter=adapter)
        assert result["arm"] == "harness" and env.log.name.startswith("waves-harness-")
        request = env.client.requests[0]
        assert request["messages"][0]["content"] == pipeline.COMMON + "\n\n" + pipeline.LOOP
        assert [t["function"]["name"] for t in request["options"]["tools"]] == [
            "write_file", "read_file", "replace_text", "inspect_page"]
        assert request["options"]["tools"] == harness.TOOLS and request["options"]["tool_choice"] == "auto"
    assert env.request["adapter"]["used"] is False and env.request["adapter"]["origin"] is None
    result = env.run([reply("done")], adapter=learned)
    assert result["arm"] == "adapted" and env.log.name.startswith("waves-adapted-")


# ---------- arm C ----------

def test_arm_c_context_package_and_wm(env, tmp_path):
    adapter = make_adapter(tmp_path / "learned", origin=True)
    result = env.run([reply(calls=[tool("wm-1", "wm", {"name": "recall", "args": {"address": "x"}}),
                                   tool("wm-2", "wm", {"name": "run", "args": {}})]),
                      reply(calls=[tool("write-1", "write_file", {"content": HTML})]), reply("done")],
                     adapter=adapter)
    context = "SYNTHETIC KNOWLEDGE PACKAGE"
    system = env.client.requests[0]["messages"][0]["content"]
    assert system == pipeline.COMMON + "\n\n" + pipeline.LOOP + "\n\n" + pipeline.BRIDGE + "\n\n" + context
    tools = env.client.requests[0]["options"]["tools"]
    assert tools[:4] == harness.TOOLS and tools[4]["function"]["name"] == "wm"
    assert tools[4]["function"]["description"] == "Synthetic world model." + harness.WM_SUFFIX
    assert "context_package" not in json.dumps(tools)
    second = env.client.requests[1]["messages"]
    answers = [json.loads(m["content"]) for m in second if m["role"] == "tool"]
    assert answers[0]["result"] == "SYNTHETIC_MEMORY" and not answers[1]["ok"]
    assert (result["arm"], result["status"], result["wm_calls"], result["tool_calls"]) == ("adapted", "completed", 2, 3)
    assert result["adapter_context_sha256"] == hashlib.sha256(context.encode()).hexdigest()
    assert env.request["adapter"]["used"] is True and env.request["adapter"]["context_sha256"] == \
        result["adapter_context_sha256"]


TEMPLATE = TASK.parents[1] / "mwm" / "visual_showcase" / "template"


def test_arm_c_with_the_real_template(env, tmp_path, monkeypatch):
    adapter = tmp_path / "learned"
    shutil.copytree(TEMPLATE, adapter, ignore=shutil.ignore_patterns("__pycache__"))
    manifest = yaml.safe_load((adapter / "adapter.yaml").read_text())
    assert "origin" not in manifest and pipeline.arm_of(TEMPLATE) == "harness"
    manifest["origin"] = {"template": manifest["template"]["id"], "version": manifest["template"]["version"]}
    (adapter / "adapter.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    monkeypatch.setattr(sys, "dont_write_bytecode", False)        # as without PYTHONDONTWRITEBYTECODE
    result = env.run([reply(calls=[tool("wm-1", "wm", {"name": "list", "args": {}}),
                                   tool("wm-2", "wm", {"name": "search", "args": {"query": "OLD_TICKS_FRAC"}})]),
                      reply("done")], adapter=adapter)
    system = env.client.requests[0]["messages"][0]["content"]
    prefix = pipeline.COMMON + "\n\n" + pipeline.LOOP + "\n\n" + pipeline.BRIDGE + "\n\n"
    assert system.startswith(prefix) and len(system) > len(prefix) + 1000
    assert result["arm"] == "adapted" and result["wm_calls"] == 2
    answers = [json.loads(m["content"]) for m in env.client.requests[1]["messages"] if m["role"] == "tool"]
    assert all(a.get("ok") is not False for a in answers), answers
    # Loading the adapter in this process left no bytecode caches in it, so wm search shows source lines only.
    assert not list(adapter.rglob("__pycache__")) and sys.dont_write_bytecode is False
    assert "staleness.py" in answers[1]["text"] and ".pyc" not in answers[1]["text"]


def test_arm_c_never_sees_the_trainers_card(env, tmp_path):
    """`<run>/final` carries CARD.md (spending; after `eval --adapter final`, the mean score). wm read and wm search
    reach every adapter file, so the pipeline loads a copy without it; the adapter given is left as it is."""
    adapter = tmp_path / "final"
    shutil.copytree(TEMPLATE, adapter, ignore=shutil.ignore_patterns("__pycache__"))
    manifest = yaml.safe_load((adapter / "adapter.yaml").read_text())
    manifest["origin"] = {"template": manifest["template"]["id"], "version": manifest["template"]["version"]}
    (adapter / "adapter.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    plain = pipeline._tree_sha256(adapter)
    (adapter / "CARD.md").write_text("# Card\n\n| final | 1 | 3 | 0.650 | CARD-MARK-31d7 |\n")
    result = env.run([reply(calls=[tool("wm-1", "wm", {"name": "read", "args": {"path": "/world-model/CARD.md"}}),
                                   tool("wm-2", "wm", {"name": "search", "args": {"query": "CARD-MARK-31d7"}}),
                                   tool("wm-3", "wm", {"name": "list", "args": {}})]),
                      reply("done")], adapter=adapter)
    assert result["arm"] == "adapted" and result["wm_calls"] == 3
    replies = [m["content"] for m in env.client.requests[1]["messages"] if m["role"] == "tool"]
    assert "not found" in replies[0] and "no matches" in replies[1] and "CARD" not in replies[2]
    assert "0.650" not in json.dumps(env.client.requests[0]["messages"][0])
    assert env.request["adapter"]["not_shown_to_agent"] == ["CARD.md"]
    assert env.request["adapter"]["path"] == str(adapter) and env.request["adapter"]["tree_sha256"] == plain
    assert (adapter / "CARD.md").is_file() and "| 0.650 |" not in all_text(env.log)


# ---------- arm B loop and budgets ----------

def test_tool_loop_images_and_latest_batch_with_matching_ids(env):
    replies = [reply(calls=[tool("write-1", "write_file", {"content": HTML}), tool("inspect-1", "inspect_page", {})]),
               reply(calls=[tool("edit-1", "replace_text", {"old": "ORIGINAL_MARK", "new": "UPDATED_MARK"}),
                            tool("inspect-2", "inspect_page", {})]),
               reply("Synthetic final summary")]
    result = env.run(replies)
    assert (result["status"], result["model_calls"], result["tool_calls"], result["inspection_calls"]) == (
        "completed", 3, 4, 2)
    assert result["final_inspection_matches"] is True
    second, third = env.client.requests[1]["messages"], env.client.requests[2]["messages"]
    assert [m["tool_call_id"] for m in second if m["role"] == "tool"] == ["write-1", "inspect-1"]
    assert [m["tool_call_id"] for m in third if m["role"] == "tool"] == ["write-1", "inspect-1", "edit-1", "inspect-2"]
    assert any(m.get("reasoning_content") == PRIVATE for m in third)    # kept in memory for the next turn
    final_ws = env.log / "workspace"
    for messages, html in ((second, HTML), (third, HTML.replace("ORIGINAL_MARK", "UPDATED_MARK"))):
        image_messages = [m for m in messages if isinstance(m.get("content"), list)]
        assert len(image_messages) == 1 and image_messages[0]["role"] == "user"   # the newest batch only
        assert image_messages[0]["content"][0]["text"] == pipeline.IMAGES_INTRO
        parts = [p for p in image_messages[0]["content"] if p["type"] == "image_url"]
        (final_ws / "probe.html").write_text(html)
        expected = [PNG + n.encode() + harness.sha(final_ws / "probe.html")[:8].encode() for n in SHOTS]
        assert [base64.b64decode(p["image_url"]["url"].split(",", 1)[1]) for p in parts] == expected
    (final_ws / "probe.html").unlink()
    assert sum(m.get("content") == pipeline.IMAGES_REMOVED for m in third) == 1
    assert (env.log / "final.md").read_text() == "Synthetic final summary"
    assert (env.log / "submission" / "index.html").read_text() == HTML.replace("ORIGINAL_MARK", "UPDATED_MARK")
    for path in sorted((env.log / "calls").glob("*.json")):
        record = json.loads(path.read_text())
        assert record["request_accepted"] and PRIVATE not in path.read_text()
        assert record["request_image_count"] == (0 if record["call"] == 1 else 5)
    assert result["usage"] == {"prompt_tokens": 120, "completion_tokens": 300, "cached_tokens": 0,
                               "reasoning_tokens": 12}


def test_twelve_request_cap_and_per_call_timeout(env):
    replies = [reply(calls=[tool(f"read-{i}", "read_file", {"path": "README.md"})]) for i in range(13)]
    result = env.run(replies)
    assert len(env.client.requests) == 12 and result["model_calls"] == 12
    assert (result["status"], result["stop_reason"]) == ("budget_exhausted", "maximum requests reached")
    assert all(0 < r["timeout"] <= 420 for r in env.client.requests)
    assert result["valid_submission"] is False


def test_cumulative_completion_budget(env):
    replies = [reply(calls=[tool(f"read-{i}", "read_file", {"path": "README.md"})], tokens=16384) for i in range(5)]
    result = env.run(replies)
    assert len(env.client.requests) == 4 and result["usage"]["completion_tokens"] == 65536
    assert [r["options"]["max_tokens"] for r in env.client.requests] == [32768, 32768, 32768, 16384]
    assert (result["status"], result["stop_reason"]) == ("budget_exhausted", "token or time budget exhausted")


def test_tool_budget_24_and_over_budget_batch(env):
    calls = [tool(f"read-{i}", "read_file", {"path": "README.md"}) for i in range(26)]
    result = env.run([reply(calls=calls)])
    assert (result["model_calls"], result["tool_calls"]) == (1, 26)
    assert (result["within_budget_tool_requests"], result["over_budget_tool_requests"]) == (24, 2)
    records = [json.loads(p.read_text()) for p in (env.log / "tools").glob("*.json")]
    assert len(records) == 26 and sum(r["result"]["ok"] for r in records) == 24
    assert (result["status"], result["stop_reason"]) == ("budget_exhausted", "tool-call budget exhausted")


def test_three_inspections_and_path_rejection_in_the_loop(env):
    calls = [tool("w", "write_file", {"path": "index.html", "content": HTML})] + \
            [tool(f"inspect-{i}", "inspect_page", {}) for i in range(4)]
    result = env.run([reply(calls=calls), reply("done")])
    assert result["inspection_calls"] == 3 and result["valid_submission"] is False
    assert result["status"] == "no_artifact"
    first = json.loads((env.log / "tools" / "01.json").read_text())
    assert not first["result"]["ok"] and "Additional properties" in first["result"]["error"]
    assert sorted(p.name for p in (env.log / "checks").iterdir()) == ["check-01", "check-02", "check-03"]


def test_final_text_is_not_saved_as_html(env):
    result = env.run([reply(HTML)])
    assert (result["status"], result["valid_submission"], result["tool_calls"]) == ("no_artifact", False, 0)


def test_api_error_is_recorded_not_raised(env):
    result = env.run([reply(calls=[tool("write-1", "write_file", {"content": HTML})]),
                      RuntimeError(f"chat/completions HTTP 503: busy {KEY}")])
    assert (result["status"], result["stop_reason"]) == ("api_error", "HTTP 503: busy [REDACTED]")
    record = json.loads((env.log / "calls" / "02.json").read_text())
    assert (record["http_status"], record["request_accepted"]) == (503, False)
    assert result["valid_submission"] is True and (env.log / "capture" / "summary.json").is_file()
    assert KEY not in all_text(env.log)


def test_authentication_failure_is_recorded_then_stops_everything(env, tmp_path):
    """The old campaign stopped all further runs on HTTP 401/403. EnvironmentFailure is a SystemExit, so the
    Trainer's run_one (which records an ordinary exception as a failed task) lets it through."""
    from harnessadapter.trainer.rollout import run_one
    with pytest.raises(pipeline.EnvironmentFailure, match="HTTP 401: bad key \\[REDACTED\\]") as stopped:
        env.run([reply(calls=[tool("write-1", "write_file", {"content": HTML})]),
                 RuntimeError(f"chat/completions HTTP 401: bad key {KEY}")])
    assert isinstance(stopped.value, SystemExit) and KEY not in str(stopped.value)
    log = next((env.root / "logs").iterdir())            # recorded in full before stopping
    result = json.loads((log / "result.json").read_text())
    assert (result["status"], result["stop_reason"]) == ("api_error", "HTTP 401: bad key [REDACTED]")
    assert (log / "capture" / "summary.json").is_file() and KEY not in all_text(log)
    env.client = FakeClient([RuntimeError("chat/completions HTTP 403: forbidden")])
    with pytest.raises(pipeline.EnvironmentFailure, match="HTTP 403"):
        run_one(dict(TASKS["waves"]), make_adapter(tmp_path / "learned", origin=True), pipeline.run,
                lambda task, out: {"score": 0.0})


def test_missing_key_with_the_real_client_stops_after_the_record(tmp_path, monkeypatch):
    monkeypatch.setenv("VISUAL_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    monkeypatch.setattr(harness, "capture", synthetic_capture)
    monkeypatch.setattr(harness, "check_browser", lambda: None)
    with pytest.raises(pipeline.EnvironmentFailure, match="FIREWORKS_API_KEY is not set"):
        pipeline.run(dict(TASKS["lava"]), None)
    result = json.loads((next((tmp_path / "logs").iterdir()) / "result.json").read_text())
    assert result["status"] == "api_error" and "FIREWORKS_API_KEY is not set" in result["stop_reason"]
    assert (result["cost_usd"], result["model_calls"]) == (0.0, 1)


def test_missing_browser_runtime_stops_before_any_run(tmp_path, monkeypatch):
    monkeypatch.setenv("VISUAL_LOG_ROOT", str(tmp_path / "logs"))

    def missing():
        raise RuntimeError("the browser capture runtime is missing: ['/Applications/Google Chrome.app']")
    monkeypatch.setattr(harness, "check_browser", missing)
    monkeypatch.setattr(pipeline, "make_client", lambda: pytest.fail("no model call without a browser"))
    for fn in (pipeline.run, pipeline.run_direct):
        with pytest.raises(pipeline.EnvironmentFailure, match="runtime is missing"):
            fn(dict(TASKS["silk"]), None)
    assert not (tmp_path / "logs").exists()


# ---------- the run log ----------

def test_log_layout_and_run_output(env):
    replies = [reply(calls=[tool("write-1", "write_file", {"content": HTML}), tool("inspect-1", "inspect_page", {})]),
               reply("done")]
    result = env.run(replies, task="silk-eval")
    log, out = env.log, env.out
    assert re.fullmatch(r"silk-harness-\d{8}T\d{6}Z-[0-9a-f]{6}", log.name) and log.parent == env.root / "logs"
    files = {p.relative_to(log).as_posix() for p in log.rglob("*") if p.is_file()}
    expected = {"case.json", "request.json", "result.json", "final.md", "calls/01.json", "calls/02.json",
                "tools/01.json", "tools/02.json", "workspace/README.md", "workspace/index.html",
                "submission/index.html", "checks/check-01/capture/summary.json",
                "checks/check-01/capture/diagnostic-paused.png", "capture/summary.json",
                "capture/diagnostic-paused.png"}
    expected |= {f"capture/{n}.png" for n in SHOTS} | {f"checks/check-01/capture/{n}.png" for n in SHOTS}
    assert files == expected
    assert json.loads((log / "case.json").read_text()) == TASKS["silk-eval"]
    assert (log / "workspace" / "README.md").read_text() == TASKS["silk"]["prompt"]
    assert set(result) == {"arm", "case", "status", "stop_reason", "finished_utc", "elapsed_seconds", "model_calls",
                           "tool_calls", "within_budget_tool_requests", "over_budget_tool_requests",
                           "inspection_calls", "wm_calls", "usage", "response_models", "adapter_context_sha256",
                           "submission_sha256", "final_inspected_sha256", "final_inspection_matches",
                           "valid_submission", "extraction", "reasoning_saved", "last_capture_not_model_feedback",
                           "cost_usd"}
    assert (result["case"], result["reasoning_saved"], result["last_capture_not_model_feedback"]) == ("silk", False, True)
    assert result["response_models"] == [pipeline.MODEL]
    assert set(env.request) == {"arm", "case", "task_id", "model", "started_utc", "system", "task", "tools", "config",
                                "adapter", "speed_gate", "capture_runtime"}
    assert env.request["task_id"] == "silk-eval" and env.request["config"] == pipeline.SETTINGS
    assert env.request["speed_gate"] is None        # the gate is off unless VISUAL_SPEED_MIN is set
    call = json.loads((log / "calls" / "01.json").read_text())
    assert call["usage"]["completion_tokens_details"]["reasoning_tokens"] == 4
    assert set(call["assistant"]) == {"role", "content", "tool_calls"}
    text = all_text(log)
    assert PRIVATE not in text and "reasoning_content" not in text and KEY not in text
    assert out.output == {"case": "silk", "arm": "harness", "status": "completed", "log_dir": str(log),
                          "submission_sha256": harness.sha(log / "submission" / "index.html")}
    assert out.messages is None and out.tokens == 80 + 200
    assert out.cost_usd == pytest.approx((80 * 0.15 + 200 * 0.5) / 1e6) == result["cost_usd"]
    assert [Path(c[1]).relative_to(log).as_posix() for c in env.captures] == ["checks/check-01/capture", "capture"]


def test_final_capture_crash_leaves_a_failed_summary(env, monkeypatch):
    def crash(*args, **kwargs):
        raise TimeoutError("synthetic capture hang")
    monkeypatch.setattr(harness, "capture", crash)
    result = env.run([reply(calls=[tool("write-1", "write_file", {"content": HTML})]), reply("done")])
    summary = json.loads((env.log / "capture" / "summary.json").read_text())
    assert result["status"] == "completed" and summary["status"] == "capture_process_failed"
    assert "synthetic capture hang" in summary["capture_errors"][0]


# ---------- the transport ----------

class FakeHTTP(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_recording_client_request_shape_metadata_and_no_retry(monkeypatch):
    monkeypatch.setenv("FIREWORKS_API_KEY", KEY)
    sent = []
    payload = {"model": pipeline.MODEL, "id": "resp-1", "choices": [{"message": {
        "role": "assistant", "content": "hi", "reasoning_content": PRIVATE}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 600},
                  "completion_tokens_details": {"reasoning_tokens": 7}}}

    def urlopen(request, timeout):
        sent.append((request, timeout))
        return FakeHTTP(json.dumps(payload).encode())
    monkeypatch.setattr(pipeline.urllib.request, "urlopen", urlopen)
    client = pipeline.make_client()
    client.timeout = 99
    message, usage = client.chat([{"role": "user", "content": "x"}], max_tokens=123, **pipeline.SAMPLING)
    request, timeout = sent[0]
    body = json.loads(request.data)
    assert request.full_url == "https://api.fireworks.ai/inference/v1/chat/completions" and timeout == 99
    assert body == {"model": pipeline.MODEL, "messages": [{"role": "user", "content": "x"}], "max_tokens": 123,
                    **pipeline.SAMPLING}
    assert request.get_header("Authorization") == f"Bearer {KEY}"
    assert (message["content"], message["reasoning_content"], message["finish_reason"]) == ("hi", PRIVATE, "stop")
    assert (usage.input_tokens, usage.cached_input_tokens, usage.output_tokens) == (1000, 600, 10)
    assert usage.cost_usd == pytest.approx((400 * 0.15 + 600 * 0.03 + 10 * 0.5) / 1e6) == client.total.cost_usd
    meta = client.last_meta
    assert (meta["http_status"], meta["response_model"], meta["response_id"]) == (200, pipeline.MODEL, "resp-1")
    assert meta["usage"]["completion_tokens_details"]["reasoning_tokens"] == 7
    assert KEY not in json.dumps(meta) and PRIVATE not in json.dumps(meta)

    def unavailable(request, timeout):
        sent.append((request, timeout))
        raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, io.BytesIO(b"overloaded"))
    monkeypatch.setattr(pipeline.urllib.request, "urlopen", unavailable)
    sent.clear()
    with pytest.raises(RuntimeError, match="HTTP 503: overloaded"):
        client.chat([{"role": "user", "content": "x"}])
    assert len(sent) == 1 and client.last_meta == {}      # max_retries=0: one attempt

    body = f"echo of {KEY} " + "x" * 5000                 # the old client kept 3,000 characters, key redacted
    monkeypatch.setattr(pipeline.urllib.request, "urlopen", lambda request, timeout: (_ for _ in ()).throw(
        urllib.error.HTTPError(request.full_url, 400, "bad", {}, io.BytesIO(body.encode()))))
    with pytest.raises(RuntimeError) as failed:
        client.chat([{"role": "user", "content": "x"}])
    detail = str(failed.value).split("HTTP 400: ", 1)[1]
    assert len(detail) == 3000 and detail.startswith("echo of [REDACTED] x") and KEY not in str(failed.value)


# ---------- through the Trainer's loader and rollout ----------

def test_rollout_through_load_callable_and_material(tmp_path, monkeypatch):
    """The Trainer's path: load pipeline.py/judge.py by file, run_one, then the material copy (judge/ included)."""
    from harnessadapter.task import load_callable
    from harnessadapter.trainer.material import Material
    from harnessadapter.trainer.rollout import run_one
    monkeypatch.setenv("VISUAL_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setenv("VISUAL_JUDGE", "mechanical")
    monkeypatch.setattr(harness, "capture", synthetic_capture)
    monkeypatch.setattr(harness, "check_browser", lambda: None)
    run = load_callable("pipeline.py:run", TASK)
    score = load_callable("judge.py:score", TASK)
    client = FakeClient([reply(calls=[tool("write-1", "write_file", {"content": HTML})]), reply("done")])
    monkeypatch.setattr(sys.modules[run.__module__], "make_client", lambda: client)
    record = run_one(dict(TASKS["waves"]), None, run, score)
    assert record["status"] == "ok" and record["output"]["arm"] == "harness"
    assert record["judge"]["feedback"] == "" and 0 <= record["judge"]["score"] <= 1
    assert record["messages"] is None and record["cost_usd"] == client.total.cost_usd
    step = Material(tmp_path / "material").write(1, 1, "c0", False, [record], None)
    copied = step / "trajectories" / "waves"
    assert (copied / "judge" / "judge.json").is_file() and (copied / "capture" / "summary.json").is_file()
    assert (copied / "submission" / "index.html").read_text() == HTML
