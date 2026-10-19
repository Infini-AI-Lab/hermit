"""The sandbox as a positive allowlist: what is bound, what is forwarded, what is recorded, and what a process inside
can actually reach."""
import http.server
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest

import harnessadapter.sandbox as sandbox_module
from harnessadapter.sandbox import (FRAMEWORK, PACKAGE, RELAY, AllowlistProxy, Sandbox, network_mode,
                                    package_closure)

REAL_WHICH = shutil.which


def fake_bwrap(monkeypatch):
    """Pretend bubblewrap is installed, to inspect the command without running it."""
    monkeypatch.setattr(sandbox_module.shutil, "which",
                        lambda name, *a, **k: "/usr/bin/bwrap" if name == "bwrap" else REAL_WHICH(name, *a, **k))


def binds(args, flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


def test_closure_has_the_framework_dependencies_and_nothing_beside_them():
    names = {d.metadata["Name"].lower() for d in package_closure(["harnessadapter"])}
    assert {"harnessadapter", "mcp", "jsonschema", "pyyaml"} <= names
    assert "pytest" not in names               # installed (the test extra), but not needed by the framework


def test_isolated_network_binds_only_what_is_granted(tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)
    granted, work = tmp_path / "granted", tmp_path / "work"
    granted.mkdir()
    proxy_url = "http://127.0.0.1:4111"
    box = Sandbox(readable=[granted], writable=[work], proxy_url=proxy_url, record_dir=tmp_path / "records")
    args, env = box.wrap([sys.executable, "-c", "pass", "--url", "http://127.0.0.1:4222/x"],
                         env={"PATH": "/usr/bin", "HA_TOOL_URL": "http://127.0.0.1:4333"})
    ro = binds(args, "--ro-bind")
    assert str(PACKAGE) in ro and str(FRAMEWORK) not in ro        # the package, not the checkout
    assert str(Path(sys.prefix).resolve()) not in ro or Path(sys.prefix) == Path(sys.base_prefix)
    site = Path(sys.prefix).resolve() / "lib"
    assert not any(p == str(site) for p in ro)                     # site-packages never as a whole
    assert str(granted.resolve()) in ro and str(work.resolve()) in binds(args, "--bind")
    assert "--unshare-net" in args
    command = args[args.index("--") + 1:]
    assert command[:2] == [sys.executable, str(RELAY)]
    forwards = [command[i + 1].partition("=")[0] for i, a in enumerate(command) if a == "--forward"]
    assert forwards == ["4111", "4222", "4333"]                    # proxy, the command's URL, the environment's URL
    assert env["HTTPS_PROXY"] == proxy_url and "127.0.0.1" in env["NO_PROXY"]
    record = json.loads((tmp_path / "records" / "learner-1.json").read_text())
    assert record["enforced"] and record["network"] == "isolated" and record["forwarded"] == [4111, 4222, 4333]
    assert record["probe"] is None and "HA_TOOL_URL" in record["environment"]
    box.close()


def test_open_network_has_no_namespace_and_no_relay(tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)
    args, _ = Sandbox(network="open").wrap(["true"])
    assert "--unshare-net" not in args and str(RELAY) not in args


def test_open_network_still_checks_the_covered_paths(tmp_path, monkeypatch):
    """A rollout's network is open by default, but its masks are still proven from inside."""
    fake_bwrap(monkeypatch)
    secret = tmp_path / "tasks.jsonl"
    secret.write_text("x")
    args, _ = Sandbox(network="open", readable=[tmp_path], masked=[secret]).wrap(["true"])
    command = args[args.index("--") + 1:]
    assert "--unshare-net" not in args and command[:2] == [sys.executable, str(RELAY)]
    assert "--open-network" in command and "--forward" not in command
    assert command[command.index("--deny") + 1] == str(secret.resolve())
    from harnessadapter.sandbox_relay import probe
    assert probe([], [], open_network=True)["outside"] == {}


def test_older_network_values():
    assert network_mode("restricted") == ("isolated", True)
    assert network_mode("none") == ("isolated", False)
    assert network_mode("open") == ("open", False)
    with pytest.raises(ValueError):
        network_mode("anything")


def test_enforcement_without_bubblewrap(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_module.shutil, "which",
                        lambda name, *a, **k: None if name == "bwrap" else REAL_WHICH(name, *a, **k))
    with pytest.raises(RuntimeError, match="best_effort"):
        Sandbox().wrap(["true"])
    args, _ = Sandbox(enforce="best_effort", record_dir=tmp_path).wrap(["true"])
    assert args == ["true"]
    record = json.loads((tmp_path / "learner-1.json").read_text())
    assert record["enforced"] is False and "bubblewrap" in record["reason"]


def test_probe_paths_skip_what_is_bound(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    box = Sandbox(probe_absent=[secret, work / "inside", tmp_path, tmp_path / "missing"])
    # tmp_path holds a bound path, work/inside lies under one, and a missing path proves nothing.
    assert box._absent([str(work.resolve())]) == [str(secret.resolve())]


def test_proxy_logs_every_decision(tmp_path):
    proxy = AllowlistProxy(["allowed.example"], log=tmp_path / "network.jsonl")
    try:
        with __import__("socket").create_connection(("127.0.0.1", proxy.server.server_address[1])) as s:
            s.sendall(b"CONNECT denied.example:443 HTTP/1.1\r\n\r\n")
            assert b"403" in s.recv(100)
    finally:
        proxy.close()
    line = json.loads((tmp_path / "network.jsonl").read_text().splitlines()[0])
    assert line["host"] == "denied.example" and line["allowed"] is False


def test_paths_are_bound_under_their_given_and_resolved_names(tmp_path, monkeypatch):
    """A path reached through a symbolic link (a venv under a linked ~/.cache) exists inside under both names: a
    program told the given name finds it, and one that resolved it first finds it too."""
    fake_bwrap(monkeypatch)
    real = tmp_path / "real"
    (real / "granted").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real)
    given, work = tmp_path / "link" / "granted", tmp_path / "link" / "work"
    box = Sandbox(readable=[given], writable=[work])
    args, env = box.wrap([sys.executable, "-c", "pass"], home=work)
    ro, rw = binds(args, "--ro-bind"), binds(args, "--bind")
    assert str(given) in ro and str(real / "granted") in ro
    assert str(work) in rw and str(real / "work") in rw
    assert env["HOME"] == str(work)                                # the name the caller uses
    box.close()


def _bwrap_works() -> bool:
    if not REAL_WHICH("bwrap"):
        return False
    return subprocess.run(["bwrap", "--unshare-user", "--unshare-net", "--ro-bind", "/", "/", "true"],
                          capture_output=True).returncode == 0


PROBE = r'''
import json, os, socket, sys, urllib.request
granted, secret, work, tool, out, linked = sys.argv[1:7]
res = {}
def t(name, f):
    try:
        res[name] = ["ok", f()]
    except Exception as e:
        res[name] = ["error", type(e).__name__]
t("read_granted", lambda: open(os.path.join(granted, "a.txt")).read())
t("read_secret", lambda: open(secret).read())
t("write_work", lambda: open(os.path.join(work, "w.txt"), "w").write("w"))
t("write_granted", lambda: open(os.path.join(granted, "w.txt"), "w").write("w"))
t("import_yaml", lambda: __import__("yaml").__name__)
t("import_pytest", lambda: __import__("pytest").__name__)
t("tool_endpoint", lambda: urllib.request.urlopen(tool, timeout=5).read().decode())
t("proxy_denied_host", lambda: urllib.request.urlopen("http://denied.example/", timeout=5).status)
t("direct_outside", lambda: socket.create_connection(("1.1.1.1", 443), timeout=3) and "connected")
t("linked_names", lambda: "both" if all(os.path.exists(os.path.join(p, "a.txt")) for p in (granted, linked)) else "one")
open(out, "w").write(json.dumps(res))
'''


@pytest.mark.skipif(not _bwrap_works(), reason="needs a working bubblewrap (unprivileged user namespaces)")
def test_inside_the_sandbox(tmp_path):
    granted, work = tmp_path / "granted", tmp_path / "work"
    granted.mkdir()
    work.mkdir()
    (granted / "a.txt").write_text("granted")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    script = granted / "probe.py"
    script.write_text(PROBE)
    (tmp_path / "link").symlink_to(tmp_path)
    linked = tmp_path / "link" / "granted"                        # the grant is given through a symbolic link

    class Tool(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"tool-ok")

        def log_message(self, *a):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Tool)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tool = f"http://127.0.0.1:{server.server_address[1]}/"
    proxy = AllowlistProxy(["allowed.example"], log=tmp_path / "records" / "network.jsonl")
    box = Sandbox(readable=[linked], writable=[work], proxy_url=proxy.url, record_dir=tmp_path / "records",
                  probe_absent=[secret])
    out = work / "result.json"
    try:
        args, env = box.wrap([sys.executable, str(script), str(granted), str(secret), str(work), tool, str(out),
                              str(linked)])
        proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-2000:]
        res = json.loads(out.read_text())
        assert res["read_granted"] == ["ok", "granted"]
        assert res["read_secret"][0] == "error"                    # not bound: it does not exist inside
        assert res["write_work"][0] == "ok" and res["write_granted"][0] == "error"
        assert res["import_yaml"] == ["ok", "yaml"]                # in the framework's dependency closure
        assert res["import_pytest"][0] == "error"                  # installed, but not shown
        assert res["tool_endpoint"] == ["ok", "tool-ok"]           # forwarded from the command's URL
        assert res["proxy_denied_host"][0] == "error"              # the proxy answers 403
        assert res["direct_outside"][0] == "error"                 # no route out of the namespace
        assert res["linked_names"] == ["ok", "both"]               # bound under the given and the resolved name
        for _ in range(50):                                        # the report arrives on its own thread
            record = json.loads((tmp_path / "records" / "learner-1.json").read_text())
            if record["probe"] is not None:
                break
            time.sleep(0.1)
        assert record["probe"]["passed"] and record["probe"]["denied"] == {str(secret.resolve()): "absent"}
        log = [json.loads(l) for l in (tmp_path / "records" / "network.jsonl").read_text().splitlines()]
        assert any(l["host"] == "denied.example" and not l["allowed"] for l in log)
    finally:
        box.close()
        proxy.close()
        server.shutdown()


def test_records_continue_after_a_resume(tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)
    Sandbox(network="open", record_dir=tmp_path).wrap(["true"])
    Sandbox(network="open", record_dir=tmp_path).wrap(["true"])    # a resumed run: a new Sandbox, same directory
    assert sorted(p.name for p in tmp_path.glob("learner-*.json")) == ["learner-1.json", "learner-2.json"]


def test_file_audit_wraps_bubblewrap_in_strace(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_module.shutil, "which",
                        lambda name, *a, **k: f"/usr/bin/{name}" if name in ("bwrap", "strace")
                        else REAL_WHICH(name, *a, **k))
    args, _ = Sandbox(network="open", record_dir=tmp_path, audit="files").wrap(["true"])
    trace = str(tmp_path / "learner-1.strace")
    assert args[:7] == ["strace", "-f", "-qq", "-e", "trace=%file", "-o", trace] and args[7] == "bwrap"
    assert json.loads((tmp_path / "learner-1.json").read_text())["audit"] == trace
    with pytest.raises(RuntimeError, match="strace"):
        Sandbox(network="open", audit="files").wrap(["true"])             # nowhere to leave the trace
    with pytest.raises(ValueError):
        Sandbox(audit="everything")


@pytest.mark.skipif(not _bwrap_works() or not REAL_WHICH("strace"), reason="needs bubblewrap and strace")
def test_file_audit_sees_what_the_sandbox_opens(tmp_path):
    granted = tmp_path / "granted"
    granted.mkdir()
    (granted / "a.txt").write_text("a")
    box = Sandbox(readable=[granted], record_dir=tmp_path / "records", audit="files")
    try:
        args, env = box.wrap([sys.executable, "-c", f"open({str(granted / 'a.txt')!r}).read()"])
        assert subprocess.run(args, env=env, capture_output=True, timeout=120).returncode == 0
    finally:
        box.close()
    assert str(granted / "a.txt") in (tmp_path / "records" / "learner-1.strace").read_text()


def test_a_granted_virtual_environment_comes_with_its_base_python(tmp_path, monkeypatch):
    """A task's external checkout runs its own .venv, whose interpreter links to an installation elsewhere."""
    fake_bwrap(monkeypatch)
    base = tmp_path / "pythons" / "cpython-3.12.4"
    (base / "bin").mkdir(parents=True)
    alias = tmp_path / "pythons" / "cpython-3.12"                 # uv's version alias, a link to the installation
    alias.symlink_to(base)
    checkout = tmp_path / "clbench"
    (checkout / ".venv").mkdir(parents=True)
    (checkout / ".venv" / "pyvenv.cfg").write_text(f"home = {alias / 'bin'}\nversion = 3.12\n")
    args, _ = Sandbox(network="open", read=[checkout]).wrap(["true"])
    ro = binds(args, "--ro-bind")
    assert str(alias) in ro and str(base.resolve()) in ro        # the interpreter links through the alias
