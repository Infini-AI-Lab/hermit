"""The sandbox as a positive allowlist: what is bound, what is forwarded, what is recorded, and what a process inside
can actually reach."""
import base64
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

import hermit.sandbox as sandbox_module
from hermit.sandbox import (FRAMEWORK, PACKAGE, RELAY, AllowlistProxy, Bind, Sandbox, network_mode,
                            package_closure)

REAL_WHICH = shutil.which


@pytest.fixture(autouse=True)
def close_the_sandboxes_a_test_makes(monkeypatch):
    """A test that wraps a command it never runs leaves a report listener: every sandbox made is closed after it."""
    made, real = [], sandbox_module.Sandbox.__post_init__

    def tracked(self):
        real(self)
        made.append(self)
    monkeypatch.setattr(sandbox_module.Sandbox, "__post_init__", tracked)
    yield
    for box in made:
        box.close()


def fake_bwrap(monkeypatch):
    """Pretend bubblewrap is installed, to inspect the command without running it."""
    monkeypatch.setattr(sandbox_module.shutil, "which",
                        lambda name, *a, **k: "/usr/bin/bwrap" if name == "bwrap" else REAL_WHICH(name, *a, **k))


def binds(args, flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


def clean_proxy_env(monkeypatch):
    """No proxy setting of the machine running the tests (NO_PROXY=*, a lowercase spelling) reaches the test."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy", "ALL_PROXY",
                 "all_proxy"):
        monkeypatch.delenv(name, raising=False)


def test_closure_has_the_framework_dependencies_and_nothing_beside_them():
    from hermit.sandbox import DISTRIBUTION
    names = {d.metadata["Name"].lower() for d in package_closure([DISTRIBUTION])}
    assert {"hermit-harness", "mcp", "jsonschema", "pyyaml", "packaging"} <= names
    assert "cryptography" in names             # through mcp's pyjwt[crypto]: a requirement's extras are followed
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
    assert str(Path(sys.prefix).resolve()) not in ro               # the installation's lib/, never all of it
    from hermit.sandbox import _environment_sites, _interpreter
    own = {os.path.realpath(p) for p in _environment_sites()}
    assert own and not own & {os.path.realpath(p) for p in binds(args, "--tmpfs")}   # the environment, as installed
    for site in _interpreter()[1]:                                 # every other interpreter's: emptied
        if os.path.realpath(site) not in own:
            assert str(site) in binds(args, "--tmpfs") and str(site) in binds(args, "--remount-ro")
    assert str(granted.resolve()) in ro and str(work.resolve()) in binds(args, "--bind")
    assert "--unshare-net" in args
    command = args[args.index("--") + 1:]
    assert command[:2] == [sys.executable, str(RELAY)]
    forwards = [command[i + 1].partition("=")[0] for i, a in enumerate(command) if a == "--forward"]
    assert forwards == ["4111", "4333"]                            # the proxy, the environment's URL (not argv's)
    assert env["HTTPS_PROXY"] == proxy_url and "127.0.0.1" in env["NO_PROXY"]
    record = json.loads((tmp_path / "records" / "learner-1.json").read_text())
    assert record["enforced"] and record["network"] == "isolated" and record["forwarded"] == [4111, 4333]
    assert record["probe"] is None and "HA_TOOL_URL" in record["environment"]
    box.close()


def test_open_network_has_no_namespace_and_no_relay(tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)
    args, _ = Sandbox(network="open").wrap(["true"])
    assert "--unshare-net" not in args and str(RELAY) not in args


def test_open_network_still_checks_the_absent_paths(tmp_path, monkeypatch):
    """An open network keeps the check inside: the paths that must be missing are proven missing."""
    fake_bwrap(monkeypatch)
    secret = tmp_path / "tasks.jsonl"
    secret.write_text("x")
    args, _ = Sandbox(network="open", probe_absent=[secret]).wrap(["true"])
    command = args[args.index("--") + 1:]
    assert "--unshare-net" not in args and command[:2] == [sys.executable, str(RELAY)]
    assert "--open-network" in command and "--forward" not in command
    assert command[command.index("--deny") + 1] == str(secret.resolve())
    from hermit.sandbox_relay import probe
    assert probe([], [], open_network=True)["outside"] == {}


def test_a_venv_granted_for_one_call_brings_its_base_installation(tmp_path, monkeypatch):
    """A sandboxed adapter server runs on the framework's Python, granted per call by a program on another
    interpreter: the venv's base installation (its pyvenv.cfg `home`) must be bound too, or Python finds no stdlib."""
    fake_bwrap(monkeypatch)
    base = tmp_path / "base"
    (base / "bin").mkdir(parents=True)
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text(f"home = {base / 'bin'}\nversion = 3.12.0\n")
    args, _ = Sandbox(network="isolated").wrap(["true"], interpreters=[venv])
    ro = binds(args, "--ro-bind")
    assert str(venv.resolve()) in ro and str(base.resolve()) in ro


def test_a_pyvenv_cfg_in_a_writable_place_shows_nothing_new(tmp_path, monkeypatch):
    """A session writes its workspace: a pyvenv.cfg it leaves there (or in a .venv) names no installation the next
    call is shown; a venv made inside is made from an installation the sandbox shows already."""
    fake_bwrap(monkeypatch)
    outside = tmp_path / "outside"
    (outside / "bin").mkdir(parents=True)
    ws = tmp_path / "ws"
    (ws / ".venv").mkdir(parents=True)
    for cfg in (ws / "pyvenv.cfg", ws / ".venv" / "pyvenv.cfg"):
        cfg.write_text(f"home = {outside / 'bin'}\nversion = 3.12\n")
    for box, kw in ((Sandbox(network="open", writable=[ws]), {}), (Sandbox(network="open"), {"writable": [ws]}),
                    (Sandbox(network="open", readable=[ws], writable=[ws]), {}),     # granted both ways
                    (Sandbox(network="open"), {"readable": [ws], "writable": [ws]}),
                    (Sandbox(network="open", readable=[ws]), {"writable": [ws]})):    # the call grants it writable
        args, _ = box.wrap(["true"], **kw)
        assert str(outside.resolve()) not in binds(args, "--ro-bind") + binds(args, "--bind")


def test_network_modes():
    assert network_mode("no-network") == ("isolated", False)
    assert network_mode("allowlist") == ("isolated", True)
    assert network_mode("public") == ("open", False)
    assert network_mode("isolated") == ("isolated", True) and network_mode("open") == ("open", False)
    for old in ("restricted", "none", "anything"):                 # the removed spellings are refused
        with pytest.raises(ValueError):
            network_mode(old)


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
    # tmp_path holds a bound path; work/inside and tmp_path/missing do not exist, and a missing path proves nothing.
    # (One that exists beneath a bound directory is kept, under its inside name too:
    # test_a_secret_under_a_renamed_bind_is_probed_under_its_inside_name.)
    assert box._absent([str(work.resolve())]) == [str(secret.resolve())]
    # A secret that is bound itself (a grant retargeted to it through a link) stays checked: the start fails.
    assert box._absent([str(work.resolve()), str(secret.resolve())]) == [str(secret.resolve())]


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
                              str(linked)], forward=[server.server_address[1]])
        proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-2000:]
        res = json.loads(out.read_text())
        assert res["read_granted"] == ["ok", "granted"]
        assert res["read_secret"][0] == "error"                    # not bound: it does not exist inside
        assert res["write_work"][0] == "ok" and res["write_granted"][0] == "error"
        assert res["import_yaml"] == ["ok", "yaml"]                # the framework's environment, as installed
        assert res["import_pytest"] == ["ok", "pytest"]            # not a dependency of the framework, installed
        assert res["tool_endpoint"] == ["ok", "tool-ok"]           # the port the caller names, forwarded
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


def test_a_view_numbers_its_records_after_the_sandboxes_own(tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)
    box = Sandbox(network="open", record_dir=tmp_path)
    box.wrap(["true"])
    view = box.view()
    box.wrap(["true"])
    view.wrap(["true"])                     # no record overwrites another's
    assert sorted(p.name for p in tmp_path.glob("learner-*.json")) == [f"learner-{i}.json" for i in (1, 2, 3)]


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
    (base / "lib" / "python3.12" / "site-packages").mkdir(parents=True)     # what is installed in the base
    (base / "lib" / "python3" / "dist-packages").mkdir(parents=True)
    args, _ = Sandbox(network="open", readable=[checkout]).wrap(["true"])
    ro = binds(args, "--ro-bind")
    assert str(alias) in ro and str(base.resolve()) in ro        # the interpreter links through the alias
    for site in (base / "lib" / "python3.12" / "site-packages", base / "lib" / "python3" / "dist-packages"):
        assert any(n in binds(args, "--tmpfs") for n in (str(site), str(site.resolve())))   # but not its packages


def test_an_adapters_venv_names_no_installation_to_show(tmp_path, monkeypatch):
    """A path given for one call (the adapter, the learner's work) is never read for a venv's home: a pyvenv.cfg
    there could name any host path."""
    fake_bwrap(monkeypatch)
    secret = tmp_path / "secrets"
    secret.mkdir()
    adapter = tmp_path / "adapter"
    (adapter / ".venv").mkdir(parents=True)
    (adapter / ".venv" / "pyvenv.cfg").write_text(f"home = {secret / 'bin'}\n")
    args, _ = Sandbox(network="open").wrap(["true"], readable=[adapter])
    assert str(secret) not in binds(args, "--ro-bind") and str(secret.resolve()) not in binds(args, "--ro-bind")


def test_a_uv_alias_inside_a_granted_checkout_is_bound_once(tmp_path, monkeypatch):
    """uv keeps a managed interpreter under the checkout itself (UV_PYTHON_INSTALL_DIR) with its version alias, a link
    beside it. The alias exists inside through the granted checkout; binding it again would fail on a link."""
    fake_bwrap(monkeypatch)
    checkout = tmp_path / "clbench"
    base = checkout / ".uv-python" / "cpython-3.13.17"
    (base / "bin").mkdir(parents=True)
    alias = checkout / ".uv-python" / "cpython-3.13"
    alias.symlink_to(base)
    (checkout / ".venv").mkdir()
    (checkout / ".venv" / "pyvenv.cfg").write_text(f"home = {alias / 'bin'}\nversion = 3.13\n")
    args, _ = Sandbox(network="open", readable=[checkout]).wrap(["true"])
    ro = binds(args, "--ro-bind")
    assert str(checkout.resolve()) in ro and str(base.resolve()) in ro
    assert str(alias) not in ro                                   # reached through the checkout, never bound itself


@pytest.mark.integration                                          # the CLI installed on the machine
@pytest.mark.skipif(not _bwrap_works(), reason="needs a working bubblewrap (unprivileged user namespaces)")
@pytest.mark.parametrize("cli", ["codex", "claude", "pi", "opencode"])
def test_an_installed_cli_starts_with_only_its_own_paths(cli):
    """The built-in CLI agents: the CLI as installed (an nvm package, a ~/bin wrapper) starts inside, with no home."""
    from hermit.sandbox import program_paths
    if not REAL_WHICH(cli):
        pytest.skip(f"{cli} is not installed")
    box = Sandbox(network="open", readable=program_paths(cli), packages=["*"])
    try:
        args, env = box.wrap([cli, "--version"])
        proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120)
    finally:
        box.close()
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert str(Path.home()) not in [a for a in args if a == str(Path.home())]   # the home is never bound whole


def test_program_paths_never_bind_the_home(tmp_path, monkeypatch):
    from hermit.sandbox import program_paths
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    tool = home / "bin" / "tool"
    tool.write_text("#!/bin/sh\necho hi\n")
    tool.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{home / 'bin'}:/usr/bin:/bin")
    paths = program_paths("tool")
    assert tool in paths and home not in paths


def named(args):
    return [(args[i], args[i + 1], args[i + 2]) for i, a in enumerate(args[:-2]) if a in ("--bind", "--ro-bind")
            and args[i + 2].startswith("/w")]


def test_binds_show_paths_under_names_of_their_own(tmp_path, monkeypatch):
    """The evolver's layout: each workspace area under a fixed name below /w; the command, the environment and the
    working directory are spelled with those names."""
    fake_bwrap(monkeypatch)
    ws, material = tmp_path / "ws", tmp_path / "material"
    (ws / "adapter").mkdir(parents=True)
    material.mkdir()
    box = Sandbox(network="open", binds=[Bind(ws / "adapter", "/w/adapter", True), Bind(material, "/w/material")],
                  layout=(ws, "/w"))
    args, env = box.wrap(["true", str(ws / "adapter" / "x.md"), f"--material={material}"],
                         env={"PATH": "/usr/bin", "WHERE": str(material / "README.md")}, chdir=ws)
    assert ("--bind", str((ws / "adapter").resolve()), "/w/adapter") in named(args)
    assert ("--ro-bind", str(material.resolve()), "/w/material") in named(args)
    command = args[args.index("--") + 1:]
    assert command[-2:] == ["/w/adapter/x.md", "--material=/w/material"]
    assert env["WHERE"] == "/w/material/README.md" and args[args.index("--chdir") + 1] == "/w"
    box.close()


def test_a_grant_under_the_layout_root_goes_where_the_layout_puts_it(tmp_path, monkeypatch):
    """A call's own writable path inside the workspace (a CLI home kept there) is shown at its layout name, so the
    HOME it is told exists inside."""
    fake_bwrap(monkeypatch)
    ws = tmp_path / "ws"
    home = ws / "agent_home"
    box = Sandbox(network="open", binds=[Bind(ws / "adapter", "/w/adapter", True)], layout=(ws, "/w"))
    args, env = box.wrap(["true"], home=home, writable=[home])
    assert named(args).count(("--bind", str(home.resolve()), "/w/agent_home")) == 1      # once, not twice
    own = [(args[i], args[i + 1], args[i + 2]) for i in range(len(args) - 2) if args[i] == "--bind"]
    assert env["HOME"] == "/w/agent_home" and ("--bind", str(home.resolve()), str(home.resolve())) not in own
    box.close()


def test_what_a_learner_is_told_and_what_it_names(monkeypatch):
    fake_bwrap(monkeypatch)                      # translation applies where the sandbox confines
    box = Sandbox(binds=[Bind(Path("/r/ws/adapter"), "/w/adapter", True), Bind(Path("/r/ev"), "/evidence")],
                  layout=(Path("/r/ws"), "/w"))
    text = "Edit /r/ws/adapter/ADAPTER.md, read /r/ev/a.json; the native /w stays; scratch /r/ws/scratch/x."
    assert box.relative(text) == ("Edit adapter/ADAPTER.md, read /evidence/a.json; the native /w stays; "
                                  "scratch scratch/x.")
    assert box.translate("/r/ws.") == "/w." and box.translate("/r/ws.bak") == "/r/ws.bak"
    assert box.outside("adapter/x") == Path("/r/ws/adapter/x")       # relative to the working directory
    assert box.outside("/evidence/a.json") == Path("/r/ev/a.json")
    assert box.outside("/elsewhere") == Path("/elsewhere")


def test_a_view_keeps_the_sandbox_and_replaces_what_it_shows(tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)
    run = Sandbox(network="no-network", binds=[Bind(tmp_path / "ws", "/w/adapter", True)], layout=(tmp_path, "/w"),
                  env_allow=["KEEP"], limits={"memory_mb": 512}, framework=False, record_dir=tmp_path / "rec",
                  forward=[3402])
    view = run.view(binds=[Bind(tmp_path / "ev", "/evidence")], readable=[tmp_path], framework=True, role="editor")
    assert view.binds == [Bind(tmp_path / "ev", "/evidence", False)] and view.layout is None
    assert (view.network, view.env_allow, view.limits, view.record_dir) == ("isolated", ["KEEP"], {"memory_mb": 512},
                                                                           tmp_path / "rec")
    assert view.framework and view.role == "editor" and view.forward == [3402]     # the model meter's port too
    assert run.binds and not run.framework                         # the run's sandbox is unchanged
    view.close()


def test_limits_go_to_the_relay(tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)
    args, _ = Sandbox(network="open", limits={"memory_mb": 1024, "cpu_s": 60}).wrap(["true"])
    command = args[args.index("--") + 1:]
    assert command[:2] == [sys.executable, str(RELAY)]
    assert [command[i + 1] for i, a in enumerate(command) if a == "--limit"] == ["cpu_s=60", "memory_mb=1024"]
    with pytest.raises(ValueError, match="unknown keys"):
        Sandbox(limits={"processes": 4})


def test_without_the_framework_only_the_relay_of_the_package_is_shown(tmp_path, monkeypatch):
    """The hermit package is not shown, its relay is; the framework's dependencies are the environment's, shown with
    it whatever `framework` says."""
    fake_bwrap(monkeypatch)
    args, _ = Sandbox(network="isolated", framework=False).wrap(["true"])
    ro = binds(args, "--ro-bind")
    assert str(PACKAGE) not in ro and str(RELAY) in ro


@pytest.mark.skipif(not _bwrap_works(), reason="needs a working bubblewrap (unprivileged user namespaces)")
def test_inside_a_layout_with_limits(tmp_path):
    ws, material = tmp_path / "ws", tmp_path / "material"
    (ws / "adapter").mkdir(parents=True)
    material.mkdir()
    (material / "a.txt").write_text("hi")
    box = Sandbox(network="isolated", binds=[Bind(ws / "adapter", "/w/adapter", True), Bind(material, "/w/material")],
                  layout=(ws, "/w"), limits={"memory_mb": 2048}, record_dir=tmp_path / "rec")
    code = ("import os, resource\nprint(os.getcwd(), open('material/a.txt').read())\n"
            "open('adapter/x', 'w').write('1')\nprint(resource.getrlimit(resource.RLIMIT_AS)[0] >> 20)")
    try:
        args, env = box.wrap([sys.executable, "-c", code], chdir=ws)
        proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120)
    finally:
        box.close()
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert proc.stdout.split() == ["/w", "hi", "2048"] and (ws / "adapter" / "x").read_text() == "1"



def test_a_file_a_sandbox_left_is_read_only_when_regular(tmp_path):
    """The host reads a learner's report or result without following a link or blocking on a pipe."""
    import os
    from hermit.sandbox import read_regular
    (tmp_path / "secret").write_text("host secret")
    (tmp_path / "report.md").write_text("done\n")
    (tmp_path / "link.md").symlink_to(tmp_path / "secret")
    os.mkfifo(tmp_path / "pipe.md")
    assert read_regular(tmp_path / "report.md") == "done\n"
    assert read_regular(tmp_path / "link.md") is None and read_regular(tmp_path / "pipe.md") is None
    assert read_regular(tmp_path / "missing.md") is None


def test_a_sanitized_copy_skips_pipes_and_caps_its_size(tmp_path):
    import os
    from hermit.isolate import sanitized_copy
    src = tmp_path / "out"
    src.mkdir()
    (src / "a.txt").write_text("x" * 100)
    os.mkfifo(src / "pipe")
    skipped = sanitized_copy(src, tmp_path / "dest")
    assert (tmp_path / "dest" / "a.txt").exists() and not (tmp_path / "dest" / "pipe").exists() and skipped
    with pytest.raises(ValueError):
        sanitized_copy(src, tmp_path / "small", max_bytes=50)
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "answer.json").write_text("gold")
    (src / "artifacts").symlink_to(hidden)               # a collected path whose parent the agent replaced
    skipped = sanitized_copy(src, tmp_path / "collected", ["artifacts/answer.json"])
    assert not (tmp_path / "collected" / "artifacts").exists() and "a link on its way" in skipped[0]


def test_a_system_pythons_site_directory_is_emptied_too(monkeypatch):
    """/usr is shown whole: a system interpreter's site directory under it is still replaced by an empty one, and the
    distributions asked for are bound back into it."""
    import glob
    found = sorted(glob.glob("/usr/lib*/python3*/site-packages"))
    if not found:
        pytest.skip("no system site-packages directory under /usr")
    site = Path(found[0])
    module = next((p for p in sorted(site.iterdir()) if p.suffix == ".py"), None)
    if module is None:
        pytest.skip("no plain module in the system site directory")
    fake_bwrap(monkeypatch)
    monkeypatch.setattr(sandbox_module, "_python_paths", lambda packages, framework=True: ([module], [site]))
    args, _ = Sandbox(network="open", framework=False).wrap(["true"])
    assert str(site) in binds(args, "--tmpfs") and str(site) in binds(args, "--remount-ro")
    assert str(module) in binds(args, "--ro-bind")          # bound back after the empty directory
    assert args.index(str(module)) > args.index("--tmpfs", args.index(str(site)) - 1)


def test_a_secret_under_a_renamed_bind_is_probed_under_its_inside_name(tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)
    method = tmp_path / "method"
    method.mkdir()
    (method / "answers.jsonl").write_text("{}")
    box = Sandbox(network="isolated", binds=[Bind(method, "/w/method")], probe_absent=[method / "answers.jsonl"])
    try:
        args, _ = box.wrap(["true"])
    finally:
        box.close()
    denied = [args[i + 1] for i, a in enumerate(args) if a == "--deny"]
    assert "/w/method/answers.jsonl" in denied and str((method / "answers.jsonl").resolve()) in denied


def test_a_path_named_inside_maps_back_under_every_bind_name(tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)                      # translation applies where the sandbox confines
    adapter, material = tmp_path / "ws" / "adapter", tmp_path / "ws" / "material"
    box = Sandbox(binds=[Bind(adapter, "/w", True), Bind(adapter, "/world-model"), Bind(material, "/w/material")],
                  layout=(adapter, "/w"))
    assert box.outside("/world-model/ADAPTER.md") == adapter / "ADAPTER.md"     # the second name of one source
    assert box.outside("/w/material/../ADAPTER.md") == adapter / "ADAPTER.md"   # normalized before mapping
    assert box.outside("material/a.md") == material / "a.md"


def test_a_file_bound_writable_under_a_name_stays_a_file(tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)
    f = tmp_path / "notes.txt"
    f.write_text("x")
    args, _ = Sandbox(network="open", binds=[Bind(f, "/w/notes.txt", True)]).wrap(["true"])
    assert f.is_file() and "/w/notes.txt" in args


def test_tools_served_to_a_sandboxed_session_name_paths_as_it_sees_them(tmp_path, monkeypatch):
    """A tool's result names host paths; through the server a sandboxed session calls, they are spelled as its
    sandbox shows them, while the tool called in this process (an Evolver on the host) keeps the host path."""
    fake_bwrap(monkeypatch)                      # translation applies where the sandbox confines
    from hermit.agent.base.tool import Tool
    from hermit.sandbox import inside_tools
    scratch = tmp_path / "ws" / "scratch"
    tool = Tool("run_agent", "x", lambda: {"log": str(scratch / "run_agent" / "t0.txt")})
    box = Sandbox(binds=[Bind(scratch, "/w/scratch", True)], layout=(tmp_path / "ws", "/w"))
    served = inside_tools({"run_agent": tool}, box)["run_agent"]
    assert served()["log"] == "/w/scratch/run_agent/t0.txt"
    assert tool()["log"] == str(scratch / "run_agent" / "t0.txt")
    assert inside_tools([tool], Sandbox(enabled=False))[0] is tool
    corpus = tmp_path / "corpus"                         # a mount of this call only, shown at /data
    found = Tool("search", "x", lambda: {"hit": str(corpus / "a.txt")})
    assert inside_tools([found], box, [(corpus, "/data", False)])[0]()["hit"] == "/data/a.txt"


def test_what_a_session_left_is_cleared_of_links_out_and_pipes(tmp_path):
    """After a sandboxed session the host reads and writes its directories (an Evolver's own code): a link out of
    them or a pipe would turn that elsewhere, so both are removed; a link inside stays."""
    import os
    from hermit.sandbox import drop_unsafe
    (tmp_path / "state" / "a").mkdir(parents=True)
    (tmp_path / "state" / "a" / "notes.md").write_text("x")
    (tmp_path / "state" / "history.md").symlink_to("/etc/hostname")        # an append would write the host file
    (tmp_path / "state" / "alias.md").symlink_to("a/notes.md")
    (tmp_path / "state" / "a" / "out").symlink_to("/etc")
    os.mkfifo(tmp_path / "state" / "a" / "report.md")
    assert sorted(drop_unsafe(tmp_path / "state")) == ["a/out", "a/report.md", "history.md"]
    assert sorted(p.name for p in (tmp_path / "state").rglob("*")) == ["a", "alias.md", "notes.md"]


def test_a_file_granted_writable_at_its_own_path_stays_a_file(tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)
    f = tmp_path / "db.sqlite"
    f.write_text("x")
    args, _ = Sandbox(network="open", writable=[f]).wrap(["true"])
    assert f.is_file() and str(f) in binds(args, "--bind")


def test_a_path_is_translated_only_where_it_starts(tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)                      # translation applies where the sandbox confines
    box = Sandbox(binds=[Bind(Path("/data"), "/w/material")], layout=(Path("/ws"), "/w"))
    assert box.translate("see /data/a and /mirror/data/b") == "see /w/material/a and /mirror/data/b"
    assert box.relative("open /ws/adapter, not /old/ws/adapter") == "open adapter, not /old/ws/adapter"


def test_tool_results_are_translated_value_by_value(tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)                      # translation applies where the sandbox confines
    from hermit.sandbox import translate_values
    scratch = tmp_path / "训练" / "scratch"
    box = Sandbox(binds=[Bind(scratch, "/w/scratch", True)])
    got = translate_values(box, {"log": str(scratch / "a.txt"), "n": 3, "logs": [str(scratch / 'b "c".txt')]})
    assert got == {"log": "/w/scratch/a.txt", "n": 3, "logs": ['/w/scratch/b "c".txt']}


def test_a_port_named_in_the_command_is_not_forwarded(tmp_path, monkeypatch):
    """A prompt in argv can carry text the learner wrote (MWM's notes): a loopback URL there opens no port; one the
    caller sets in the environment, or names, does."""
    fake_bwrap(monkeypatch)
    box = Sandbox(network="isolated", env_allow=["TOOL_URL"])
    try:
        args, _ = box.wrap(["true", "see http://127.0.0.1:5432/x"], env={"TOOL_URL": "http://127.0.0.1:4100/"},
                           forward=[4200])
    finally:
        box.close()
    forwarded = [a.split("=")[0] for i, a in enumerate(args) if i and args[i - 1] == "--forward"]
    assert forwarded == ["4100", "4200"]


def test_a_namespace_package_shows_only_the_distributions_own_part(tmp_path):
    """google-cloud-storage installs under google/, a namespace shared with other distributions: only its own
    regular package is shown, not the namespace whole."""
    from hermit.sandbox import distribution_paths
    for pkg in ("storage", "bigquery"):
        (tmp_path / "google" / "cloud" / pkg).mkdir(parents=True)
        (tmp_path / "google" / "cloud" / pkg / "__init__.py").write_text("")
    (tmp_path / "google_cloud_storage-1.0.dist-info").mkdir()

    class Dist:
        files = ["google/cloud/storage/__init__.py", "google_cloud_storage-1.0.dist-info/RECORD"]

        def locate_file(self, f):
            return tmp_path / f
    assert distribution_paths([Dist()]) == sorted([tmp_path / "google" / "cloud" / "storage",
                                                   tmp_path / "google_cloud_storage-1.0.dist-info"])


def test_the_closure_follows_extras_asked_for_after_a_first_visit(monkeypatch):
    """application -> bridge, library[feature]; bridge -> library: library's feature dependency is needed whichever
    way library is reached first."""
    import importlib.metadata as md
    from hermit.sandbox import package_closure
    requires = {"application": ["library[feature]", "bridge"], "bridge": ["library"],
                "library": ['feature-dep; extra == "feature"'], "feature-dep": []}

    class Dist:
        def __init__(self, name):
            self.name, self.requires = name, requires[name]
    monkeypatch.setattr(md, "distribution", lambda name: Dist(name) if name in requires else
                        (_ for _ in ()).throw(md.PackageNotFoundError(name)))
    assert {d.name for d in package_closure(["application"])} == set(requires)


def test_an_editable_install_shows_its_package_not_the_checkout(tmp_path):
    """A plain .pth path is often a whole checkout (data, earlier runs beside the code): only the distribution's own
    top-level package is shown."""
    from hermit.sandbox import _dist_sources
    checkout = tmp_path / "benchmark"
    (checkout / "benchmark").mkdir(parents=True)
    (checkout / "benchmark" / "__init__.py").write_text("")
    (checkout / "runs" / "old").mkdir(parents=True)
    (checkout / "runs" / "old" / "steps.jsonl").write_text("{}")
    site = tmp_path / "site"
    site.mkdir()
    (site / "__editable__.benchmark.pth").write_text(str(checkout) + "\n")

    class Dist:
        files = ["__editable__.benchmark.pth"]
        metadata = {"Name": "benchmark"}

        def locate_file(self, f):
            return site / f

        def read_text(self, name):
            return None
    assert _dist_sources(Dist()) == [(checkout / "benchmark").resolve()]


def test_every_python_versions_site_directory_under_the_prefix_is_emptied(tmp_path, monkeypatch):
    import hermit.sandbox as sb
    other = tmp_path / "lib" / "python3.11" / "site-packages"
    other.mkdir(parents=True)
    monkeypatch.setattr(sb.sys, "base_prefix", str(tmp_path))
    monkeypatch.setattr(sb.sys, "prefix", str(tmp_path))
    assert other in sb._interpreter()[1]


def test_an_editable_finder_maps_packages_and_module_files_not_their_checkout(tmp_path):
    """Current setuptools writes `MAPPING: dict[str, str] = {...}`: its packages are shown, and a module mapped by
    file is shown alone, not the directory around it."""
    from hermit.sandbox import _editable_finder_paths
    checkout = tmp_path / "checkout"
    (checkout / "pkg").mkdir(parents=True)
    (checkout / "mod.py").write_text("")
    (checkout / "runs").mkdir()
    site = tmp_path / "site"
    site.mkdir()
    (site / "__editable___x_finder.py").write_text(
        f"MAPPING: dict[str, str] = {{'pkg': {str(checkout / 'pkg')!r}, 'mod': {str(checkout / 'mod')!r}}}\n")
    got = _editable_finder_paths(site, "import __editable___x_finder; __editable___x_finder.install()")
    assert got == [checkout / "pkg", checkout / "mod.py"]


def test_a_pkgutil_namespace_is_not_shown_whole(tmp_path):
    """opencensus/__init__.py only extends the path: another distribution's opencensus/trace stays out."""
    from hermit.sandbox import distribution_paths
    (tmp_path / "opencensus" / "common" / "runtime_context").mkdir(parents=True)
    (tmp_path / "opencensus" / "trace").mkdir()
    for d in ("opencensus", "opencensus/common"):
        (tmp_path / d / "__init__.py").write_text("__path__ = __import__('pkgutil').extend_path(__path__, __name__)\n")
    (tmp_path / "opencensus" / "common" / "runtime_context" / "__init__.py").write_text("")

    class Dist:
        files = ["opencensus/common/runtime_context/__init__.py"]

        def locate_file(self, f):
            return tmp_path / f
    assert distribution_paths([Dist()]) == sorted([tmp_path / "opencensus" / "__init__.py",
                                                   tmp_path / "opencensus" / "common" / "__init__.py",
                                                   tmp_path / "opencensus" / "common" / "runtime_context"])


def test_the_allowlist_proxy_goes_out_through_the_hosts_own_proxy(tmp_path, monkeypatch):
    """On a machine whose only way out is an egress proxy (docker/offline.sh), the sandbox's allowlist proxy reaches
    an allowed host through it."""
    clean_proxy_env(monkeypatch)
    import http.server
    import threading
    import urllib.request

    class Target(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = "localhost."                                       # a name the proxies see as a host, not the loopback
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    egress = AllowlistProxy([host], log=tmp_path / "egress.jsonl")
    monkeypatch.setenv("HTTPS_PROXY", egress.url)
    inner = AllowlistProxy([host])
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": inner.url}))
        assert opener.open(f"http://{host}:{server.server_address[1]}/", timeout=10).read() == b"ok"
        assert '"CONNECT"' in (tmp_path / "egress.jsonl").read_text()      # it went out through the egress proxy
    finally:
        inner.close()
        egress.close()
        server.shutdown()


def test_the_allowlist_proxy_reaches_no_proxy_hosts_directly_and_signs_in_to_the_upstream(monkeypatch):
    """A host NO_PROXY names is reached directly, not through the machine's egress proxy; a proxy URL with
    credentials (http://user:pass@proxy) gets them on its CONNECT."""
    clean_proxy_env(monkeypatch)
    import threading
    seen = []
    upstream = socket.socket()
    upstream.bind(("127.0.0.1", 0))
    upstream.listen()

    def accept():
        conn, _ = upstream.accept()
        seen.append(conn.recv(4096))
        conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        conn.close()
    threading.Thread(target=accept, daemon=True).start()
    target = socket.socket()
    target.bind(("127.0.0.1", 0))
    target.listen()
    monkeypatch.setenv("HTTPS_PROXY", f"http://me:p%40ss@127.0.0.1:{upstream.getsockname()[1]}")
    monkeypatch.setenv("NO_PROXY", "direct.internal,ported.internal:8000")
    proxy = AllowlistProxy(["direct.internal", "via.proxy"])
    try:
        monkeypatch.setattr(socket, "create_connection", lambda address, timeout=None, _real=socket.create_connection:
                            _real(("127.0.0.1", target.getsockname()[1])
                                  if address[0] in ("direct.internal", "ported.internal") else address, timeout))
        proxy._connect("direct.internal", 443).close()          # straight to the host: the upstream saw nothing
        proxy._connect("ported.internal", 8000).close()         # NO_PROXY's host:port entry too
        assert seen == []
        with pytest.raises(OSError, match="refused"):
            proxy._connect("via.proxy", 443)
        assert b"CONNECT via.proxy:443" in seen[0]
        assert b"Proxy-Authorization: Basic " + base64.b64encode(b"me:p@ss") in seen[0]
    finally:
        proxy.close()
        upstream.close()
        target.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl for a test certificate")
def test_the_allowlist_proxy_speaks_tls_to_an_https_upstream(tmp_path, monkeypatch):
    """HTTPS_PROXY=https://proxy: the tunnel to the proxy is TLS (port 443 when none is given), CONNECT inside it."""
    import ssl
    import threading
    clean_proxy_env(monkeypatch)
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    upstream = socket.socket()
    upstream.bind(("127.0.0.1", 0))
    upstream.listen()
    seen = []

    def accept():
        conn = context.wrap_socket(upstream.accept()[0], server_side=True)
        seen.append(conn.recv(4096))
        conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        conn.sendall(conn.recv(4096))                           # the tunnel: an echo
        conn.close()
    threading.Thread(target=accept, daemon=True).start()
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    monkeypatch.setenv("HTTPS_PROXY", f"https://localhost:{upstream.getsockname()[1]}")
    proxy = AllowlistProxy(["via.proxy"])
    try:
        sock = proxy._connect("via.proxy", 443)
        sock.sendall(b"ping")
        assert sock.recv(4096) == b"ping" and b"CONNECT via.proxy:443" in seen[0]
        sock.close()
    finally:
        proxy.close()
        upstream.close()


def test_a_plain_http_request_reaches_its_server_whole_and_in_origin_form(monkeypatch):
    """A model on plain HTTP (http://inference.internal:8000/v1) goes through the allowlist proxy: the server gets
    the path it routes on, not the proxy's absolute URI, and the whole body the client sent."""
    clean_proxy_env(monkeypatch)
    import urllib.request
    seen = {}

    class Target(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen["path"] = self.path
            seen["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        monkeypatch.delenv(key, raising=False)
    proxy = AllowlistProxy(["localhost."])
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy.url}))
        body = json.dumps({"messages": [{"role": "user", "content": "x" * 5000}]}).encode()
        url = f"http://localhost.:{server.server_address[1]}/v1/chat/completions?a=1"
        assert opener.open(urllib.request.Request(url, data=body), timeout=10).read() == b"ok"
        assert seen == {"path": "/v1/chat/completions?a=1", "body": body}
    finally:
        proxy.close()
        server.shutdown()


def test_a_kept_alive_client_connection_does_not_carry_a_second_request_past_the_check(monkeypatch):
    """httpx and litellm keep their connection to an HTTP proxy alive: the proxy closes each plain request's server
    connection, so the next request comes to it again (checked, in origin form) instead of riding the first one."""
    import http.client
    paths = []

    class Target(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):                        # as uvicorn answers: a request to close is told it is closed
            paths.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            if self.headers.get("Connection", "").lower() == "close":
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"ok")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        monkeypatch.delenv(key, raising=False)
    proxy = AllowlistProxy(["localhost."])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(proxy.url.rsplit(":", 1)[1]), timeout=10)
        for name in ("a", "b"):
            conn.request("GET", f"http://localhost.:{server.server_address[1]}/{name}",
                         headers={"Connection": "keep-alive"})
            assert conn.getresponse().read() == b"ok"
        conn.close()
        assert paths == ["/a", "/b"]
    finally:
        proxy.close()
        server.shutdown()


def test_a_sandbox_without_a_proxy_drops_an_outer_ones_variables(monkeypatch):
    """A nested sandbox (the adapter's, inside the task sandbox) inherits HTTPS_PROXY pointing at the outer allowlist
    proxy; without NO_PROXY a loopback request would go there and be refused."""
    fake_bwrap(monkeypatch)
    box = Sandbox(network="isolated", env_allow=["HTTPS_PROXY", "EMBED_URL"])
    try:
        _, env = box.wrap(["true"], env={"HTTPS_PROXY": "http://127.0.0.1:4111", "EMBED_URL": "http://127.0.0.1:3611/v1"})
    finally:
        box.close()
    assert "HTTPS_PROXY" not in env and env["EMBED_URL"] == "http://127.0.0.1:3611/v1"


def test_a_name_inside_that_is_another_sources_path_is_not_translated_twice(monkeypatch):
    """Binds /run/material -> /evidence and /evidence -> /environment/evidence: /run/material/t1.json is
    /evidence/t1.json inside, not translated a second time into /environment/evidence/t1.json."""
    fake_bwrap(monkeypatch)
    box = Sandbox(binds=[Bind(Path("/run/material"), "/evidence"), Bind(Path("/evidence"), "/environment/evidence")],
                  layout=(Path("/run/ws"), "/w"))
    text = "read /run/material/t1.json and /evidence/x.json"
    assert box.translate(text) == "read /evidence/t1.json and /environment/evidence/x.json"
    assert box.relative(text) == "read /evidence/t1.json and /environment/evidence/x.json"
    assert box.relative("edit /run/ws/adapter/ADAPTER.md in /run/ws") == "edit adapter/ADAPTER.md in ."
    box.close()


def _fake_package(root):
    """pkg/ on a path of its own: an __init__ that fails when run, a scoring module with a bytecode cache, a data/
    subpackage."""
    pkg = root / "pkg"
    (pkg / "__pycache__").mkdir(parents=True)
    (pkg / "data").mkdir()
    (pkg / "__init__.py").write_text("raise RuntimeError('imported')\n")
    (pkg / "scoring.py").write_text("ANSWER = 42\n")
    (pkg / "__pycache__" / "scoring.cpython-312.pyc").write_bytes(b"cache")
    (pkg / "data" / "answers.json").write_text("{}")
    return pkg


def test_hide_names_resolve_to_files_without_importing(tmp_path, monkeypatch):
    from hermit.sandbox import hidden_paths
    pkg = _fake_package(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    found = hidden_paths(["pkg.scoring", "pkg.data"])            # the failing __init__ never runs
    assert found == [pkg / "scoring.py", pkg / "data"]
    assert hidden_paths(["pkg", "pkg.scoring"]) == [pkg]         # inside a hidden directory: that one covers it
    other = _fake_package(tmp_path / "second")                   # a second copy on the path: hidden too
    monkeypatch.syspath_prepend(str(tmp_path / "second"))
    assert set(hidden_paths(["pkg.scoring"])) == {pkg / "scoring.py", other / "scoring.py"}
    with pytest.raises(ValueError, match="not installed"):
        hidden_paths(["pkg.missing"])
    with pytest.raises(ValueError, match="not installed"):
        hidden_paths(["no_such_package_anywhere"])


def test_hide_covers_what_is_shown_and_records_it(tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)
    pkg = _fake_package(tmp_path / "lib")
    elsewhere = tmp_path / "elsewhere.py"
    elsewhere.write_text("")
    box = Sandbox(network="open", readable=[tmp_path / "lib"], record_dir=tmp_path / "rec",
                  hide=[pkg / "scoring.py", pkg / "data", elsewhere])
    args, _ = box.wrap(["true"])
    i = args.index(str(pkg / "scoring.py"))
    assert args[i - 2] == "--ro-bind" and Path(args[i - 1]).read_text() == ""     # a file: an empty one
    j = args.index(str(pkg / "data"), args.index(str(tmp_path / "lib")))
    assert args[j - 1] == "--tmpfs" and args[j + 1:j + 3] == ["--remount-ro", str(pkg / "data")]
    assert str(elsewhere) not in args                                     # not shown: nothing to cover
    assert args.index(str(pkg / "scoring.py")) > args.index(str(tmp_path / "lib"))   # covered after it is bound
    k = args.index(str(pkg / "__pycache__"), args.index(str(tmp_path / "lib")))    # its bytecode, written later too
    assert args[k - 1] == "--tmpfs"
    record = json.loads((tmp_path / "rec" / "learner-1.json").read_text())
    assert record["hidden"] == [str(pkg / "scoring.py"), str(pkg / "__pycache__"), str(pkg / "data")]
    box.close()


def test_the_hide_list_comes_from_the_run_through_the_environment(monkeypatch, tmp_path):
    from hermit.sandbox import ENV_HIDE, set_hidden
    set_hidden([tmp_path / "a.py", tmp_path / "b"])
    try:
        assert Sandbox().hide == [tmp_path / "a.py", tmp_path / "b"]       # a sandbox a child process makes
        assert Sandbox(hide=[]).hide == []                                 # the judge's
    finally:
        set_hidden([])
    assert ENV_HIDE not in os.environ and Sandbox().hide == []


def test_without_the_framework_a_hermit_installed_in_the_environment_is_covered(tmp_path, monkeypatch):
    """A hermit installed in site-packages (not editable) lies in the environment every sandbox shows: without the
    framework it is covered, and the relay inside it is bound again."""
    fake_bwrap(monkeypatch)
    site = tmp_path / "site-packages"
    package = site / "hermit"
    package.mkdir(parents=True)
    (package / "sandbox_relay.py").write_text("")
    monkeypatch.setattr(sandbox_module, "PACKAGE", package)
    monkeypatch.setattr(sandbox_module, "RELAY", package / "sandbox_relay.py")
    monkeypatch.setattr(sandbox_module, "_python_paths", lambda packages: ([site], []))
    args, _ = Sandbox(network="isolated", framework=False).wrap(["true"])
    i = args.index("--tmpfs", args.index(str(site)))
    relay = str(package / "sandbox_relay.py")
    # bound again into the empty package, before it is made read-only (bwrap makes the mount point there)
    assert args[i:i + 7] == ["--tmpfs", str(package), "--ro-bind", relay, relay, "--remount-ro", str(package)]
    args, _ = Sandbox(network="isolated", framework=True).wrap(["true"])
    assert ["--tmpfs", str(package)] not in [args[k:k + 2] for k in range(len(args))]


HIDDEN_PROBE = r'''
import importlib.util, json, os, sys
pkg, framework, out = sys.argv[1:4]
sys.path.insert(0, os.path.dirname(pkg))
res = {}
def t(name, f):
    try:
        res[name] = ["ok", f()]
    except Exception as e:
        res[name] = ["error", type(e).__name__]
t("read_module", lambda: open(os.path.join(pkg, "scoring.py")).read())
t("read_cache", lambda: open(os.path.join(pkg, "__pycache__", "scoring.cpython-312.pyc"), "rb").read().decode())
t("list_data", lambda: os.listdir(os.path.join(pkg, "data")))
t("read_other", lambda: open(os.path.join(pkg, "__init__.py")).read()[:5])
t("import_module", lambda: importlib.util.find_spec("pkg.scoring") is not None)
t("checkout", lambda: sorted(os.listdir(framework)))
t("import_numpy", lambda: importlib.util.find_spec("numpy") is not None)
open(out, "w").write(json.dumps(res))
'''


@pytest.mark.skipif(not _bwrap_works(), reason="needs a working bubblewrap (unprivileged user namespaces)")
def test_inside_the_sandbox_the_hidden_paths_are_empty(tmp_path):
    lib, work = tmp_path / "lib", tmp_path / "work"
    pkg = _fake_package(lib)
    (pkg / "__init__.py").write_text("")
    work.mkdir()
    script = work / "probe.py"
    script.write_text(HIDDEN_PROBE)
    box = Sandbox(network="open", readable=[lib], writable=[work],
                  hide=[pkg / "scoring.py", pkg / "__pycache__" / "scoring.cpython-312.pyc", pkg / "data"])
    try:
        args, env = box.wrap([sys.executable, str(script), str(pkg), str(FRAMEWORK), str(work / "out.json")])
        proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-2000:]
    finally:
        box.close()
    res = json.loads((work / "out.json").read_text())
    assert res["read_module"] == ["ok", ""]                   # an empty file
    assert res["read_cache"][0] == "error"                    # the whole __pycache__: an empty directory
    assert res["list_data"] == ["ok", []]
    assert res["read_other"] == ["ok", ""]                    # the package's other files stay
    assert res["import_module"] == ["ok", True]              # an empty module: none of its names
    import importlib.util
    if FRAMEWORK != PACKAGE.parent or not str(FRAMEWORK).endswith("-packages"):
        assert res["checkout"] == ["ok", ["hermit"]]          # an editable checkout: the package alone
    if importlib.util.find_spec("numpy") is not None:
        assert res["import_numpy"] == ["ok", True]           # installed beside the framework: shown


@pytest.mark.skipif(not _bwrap_works(), reason="needs a working bubblewrap (unprivileged user namespaces)")
def test_inside_the_sandbox_an_installed_hermit_is_covered_but_the_relay_runs(tmp_path, monkeypatch):
    site = tmp_path / "site-packages"
    package = site / "hermit"
    package.mkdir(parents=True)
    shutil.copy(RELAY, package / "sandbox_relay.py")
    (package / "trainer.py").write_text("SECRET = 1\n")
    (package / "runtime").mkdir()
    (package / "runtime" / "tool.py").write_text("")
    real = sandbox_module._python_paths
    monkeypatch.setattr(sandbox_module, "PACKAGE", package)
    monkeypatch.setattr(sandbox_module, "RELAY", package / "sandbox_relay.py")
    monkeypatch.setattr(sandbox_module, "_python_paths", lambda packages: (real(packages)[0] + [site],
                                                                           real(packages)[1]))
    box = Sandbox(network="isolated", framework=False, readable=[package / "runtime"])   # a method's helpers
    try:
        args, env = box.wrap([sys.executable, "-c", f"import os; print(sorted(os.listdir({str(package)!r})))"])
        proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120)
    finally:
        box.close()
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip() == "['runtime', 'sandbox_relay.py']"          # granted parts stay, the rest is gone


def test_hide_covers_every_name_a_hidden_path_is_shown_by(tmp_path, monkeypatch):
    """A directory bound under a linked name and under a bind's target shows the hidden file there too: it is covered
    under each."""
    fake_bwrap(monkeypatch)
    pkg = _fake_package(tmp_path / "real")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    box = Sandbox(network="open", readable=[tmp_path / "link"], binds=[Bind(tmp_path / "real", "/w/lib")],
                  hide=[pkg / "scoring.py"])
    args, _ = box.wrap(["true"])
    covered = [args[i + 2] for i in range(len(args) - 2) if args[i] == "--ro-bind" and args[i + 1].startswith(
        tempfile.gettempdir()) and Path(args[i + 1]).name.startswith("ha-empty-")]
    assert set(covered) == {str(pkg / "scoring.py"), str(tmp_path / "link" / "pkg" / "scoring.py"),
                            "/w/lib/pkg/scoring.py"}
    assert args.index("/w/lib/pkg/scoring.py") > args.index("/w/lib")     # after the bind it lies in
    box.close()


def test_hide_covers_a_bind_of_the_hidden_path_or_of_something_in_it(tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)
    pkg = _fake_package(tmp_path / "lib")
    box = Sandbox(network="open", binds=[Bind(pkg / "data", "/w/environment/data"),
                                         Bind(pkg / "data" / "answers.json", "/w/answers.json")],
                  hide=[pkg / "data"])
    args, _ = box.wrap(["true"])
    i = args.index("/w/environment/data", args.index("/w/environment/data") + 1)   # after its own bind
    assert args[i - 1] == "--tmpfs" and args[i + 1:i + 3] == ["--remount-ro", "/w/environment/data"]
    j = args.index("/w/answers.json", args.index("/w/answers.json") + 1)
    assert args[j - 2] == "--ro-bind" and Path(args[j - 1]).read_text() == ""
    box.close()


def test_a_declared_venv_made_from_this_installation_keeps_its_site_directory(tmp_path, monkeypatch):
    """A benchmark's venv made from the interpreter the framework runs on names that installation: its site
    directory is the environment's own, shown as installed, not emptied."""
    from hermit.sandbox import _environment_sites
    fake_bwrap(monkeypatch)
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text(f"home = {Path(sys.executable).resolve().parent}\n")
    args, _ = Sandbox(network="open", readable=[venv]).wrap(["true"])
    own = {os.path.realpath(p) for p in _environment_sites()}
    assert own and not own & {os.path.realpath(p) for p in binds(args, "--tmpfs")}


def test_the_hide_list_passes_every_environment_filter(tmp_path, monkeypatch):
    fake_bwrap(monkeypatch)
    _, env = Sandbox(network="open", env_allow=[]).wrap(["true"], env={"HA_SANDBOX_HIDE": "/x/a.py", "OTHER": "1"})
    assert env.get("HA_SANDBOX_HIDE") == "/x/a.py" and "OTHER" not in env


def test_hide_covers_a_grant_inside_a_hidden_directory_shown_only_through_it(tmp_path, monkeypatch):
    """The hidden directory itself is not shown, a path inside it is granted at its own place: that grant is
    covered (nothing else of the directory is there to cover)."""
    fake_bwrap(monkeypatch)
    pkg = _fake_package(tmp_path / "lib")
    box = Sandbox(network="open", readable=[pkg / "data"], hide=[pkg])
    args, _ = box.wrap(["true"])
    assert ["--tmpfs", str(pkg / "data"), "--remount-ro", str(pkg / "data")] in \
        [args[k:k + 4] for k in range(len(args))]
    assert str(pkg) not in binds(args, "--tmpfs")
    box.close()


def test_without_the_framework_its_own_grants_stay(tmp_path, monkeypatch):
    """A method grants parts of the hermit package to a view without the framework (MWM: its runtime, its bin/):
    covering the package leaves them, only sandbox.hide covers a grant."""
    fake_bwrap(monkeypatch)
    site = tmp_path / "site-packages"
    package = site / "hermit"
    (package / "runtime").mkdir(parents=True)
    (package / "sandbox_relay.py").write_text("")
    monkeypatch.setattr(sandbox_module, "PACKAGE", package)
    monkeypatch.setattr(sandbox_module, "RELAY", package / "sandbox_relay.py")
    monkeypatch.setattr(sandbox_module, "_python_paths", lambda packages: ([], []))
    args, _ = Sandbox(network="isolated", framework=False, readable=[package / "runtime"]).wrap(["true"])
    assert str(package / "runtime") in binds(args, "--ro-bind")
    assert str(package / "runtime") not in binds(args, "--tmpfs")
    args, _ = Sandbox(network="isolated", framework=False, readable=[package / "runtime"],
                      hide=[package / "runtime"]).wrap(["true"])
    assert str(package / "runtime") in binds(args, "--tmpfs")


def test_a_hidden_path_the_check_must_find_absent_is_covered_instead(tmp_path, monkeypatch):
    """The judge's own file hidden through sandbox.hide: covered (empty inside), so the check does not ask it to
    be absent, which a covered file never is."""
    fake_bwrap(monkeypatch)
    pkg = _fake_package(tmp_path / "lib")
    box = Sandbox(network="isolated", readable=[tmp_path / "lib"], hide=[pkg / "scoring.py"],
                  probe_absent=[pkg / "scoring.py", pkg / "data" / "answers.json"])
    args, _ = box.wrap(["true"])
    command = args[args.index("--") + 1:]
    denied = [command[i + 1] for i, a in enumerate(command) if a == "--deny"]
    assert str(pkg / "scoring.py") not in denied and str(pkg / "data" / "answers.json") in denied
    box.close()


def test_hide_finds_an_untagged_extension_module(tmp_path, monkeypatch):
    from hermit.sandbox import hidden_paths
    pkg = _fake_package(tmp_path)
    (pkg / "native.so").write_bytes(b"")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert hidden_paths(["pkg.native"]) == [pkg / "native.so"]


@pytest.mark.skipif(not _bwrap_works(), reason="needs a working bubblewrap (unprivileged user namespaces)")
def test_the_command_line_runs_a_command_with_a_socket_forwarded_and_a_path_hidden(tmp_path):
    """`python -m hermit.sandbox SPEC -- COMMAND` (a launcher's command prefix): a Unix socket outside answers at its
    port inside, what `hide` names is empty, the call's HOME is kept, the exit status comes back."""
    import socketserver
    sock, hidden = tmp_path / "svc.sock", tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "answer.txt").write_text("42")

    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(b"svc-ok")

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
    server = Server(str(sock), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    spec = {"network": "no-network", "readable": [str(tmp_path)], "hide": [str(hidden)], "sockets": {"3130": str(sock)},
            "env": {"HOME": "/tmp", "LANG": "C.UTF-8"}, "chdir": str(tmp_path), "framework": False}
    code = ("import os, socket; s = socket.create_connection(('127.0.0.1', 3130), timeout=5); "
            "print(s.recv(10).decode(), os.listdir('hidden'), os.environ['HOME'], os.getcwd()); raise SystemExit(3)")
    try:
        proc = subprocess.run([sys.executable, "-m", "hermit.sandbox", json.dumps(spec), "--", sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=120,
                              env={**os.environ, "PYTHONPATH": str(FRAMEWORK)})
    finally:
        server.shutdown()
    assert proc.returncode == 3, proc.stderr[-2000:]
    assert proc.stdout.split() == ["svc-ok", "[]", "/tmp", str(tmp_path)]


def test_hide_names_must_be_module_names_and_find_bytecode_only_modules(tmp_path, monkeypatch):
    from hermit.sandbox import hidden_paths
    pkg = _fake_package(tmp_path)
    (pkg / "compiled.pyc").write_bytes(b"")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert hidden_paths(["pkg.compiled"]) == [pkg / "compiled.pyc"]
    for bad in ("", "pkg..x", "../etc", "pkg.scoring "):
        with pytest.raises(ValueError, match="not a module or package name"):
            hidden_paths([bad])


def test_the_command_line_keeps_the_runs_hide_list(tmp_path, monkeypatch):
    """`python -m hermit.sandbox` adds SPEC's hides to the run's (HA_SANDBOX_HIDE), never replaces them; a record
    directory given as text is a path."""
    from hermit.sandbox import ENV_HIDE, _main
    made = {}

    class Box:
        def __init__(self, **kw):
            made.update(kw)

        def wrap(self, command, **kw):
            return ["true"], {}

        def close(self):
            pass
    monkeypatch.setattr(sandbox_module, "Sandbox", Box)
    monkeypatch.setenv(ENV_HIDE, "/run/hidden.py")
    assert _main([json.dumps({"hide": ["/spec/hidden"], "record_dir": str(tmp_path)}), "--", "true"]) == 0
    assert made["hide"] == [Path("/run/hidden.py"), Path("/spec/hidden")] and made["record_dir"] == tmp_path


def test_a_loopback_url_without_a_port_forwards_its_schemes_port(monkeypatch):
    box = Sandbox(network="isolated")
    env = {"A": "http://127.0.0.1/v1", "B": "https://localhost/", "C": "http://localhost:8000/x",
           "D": "http://127.0.0.1.example.com/", "E": "http://localhost"}
    assert box.forwarded_ports(["true"], env) == [80, 443, 8000]


def test_a_report_that_will_not_come_has_its_listener_closed(tmp_path, monkeypatch):
    """A command wrapped but never run (its deadline passed): abandoning it closes the listener for its report."""
    fake_bwrap(monkeypatch)
    box = Sandbox(network="isolated")
    try:
        args, _ = box.wrap(["true"])
        report = args[args.index("--report") + 1]
        assert os.path.exists(report)
        box.abandon(args)
        for _ in range(50):
            if not os.path.exists(report):
                break
            time.sleep(0.05)
        assert not os.path.exists(report) and not box._bridges._reports
    finally:
        box.close()


def test_a_model_only_call_forwards_no_loopback_url_of_its_environment(monkeypatch):
    fake_bwrap(monkeypatch)
    box = Sandbox(network="isolated", env_allow=["SERVICE_URL"])
    try:
        args, _ = box.wrap(["true"], env={"SERVICE_URL": "http://127.0.0.1:5432"}, env_ports=False)
        command = args[args.index("--") + 1:]
        assert "--forward" not in command
        args, _ = box.wrap(["true"], env={"SERVICE_URL": "http://127.0.0.1:5432"})
        assert any(a.startswith("5432=") for a in args)
    finally:
        box.close()


def test_a_trusted_interpreters_base_installation_shows_no_packages_of_its_own(tmp_path, monkeypatch):
    """An adapter server on another interpreter names its venv as trusted: the installation it was made from is
    shown for its interpreter, its site-packages emptied."""
    fake_bwrap(monkeypatch)
    base = tmp_path / "base"
    site = base / "lib" / "python3.12" / "site-packages"
    (site / "foreign").mkdir(parents=True)
    (base / "bin").mkdir()
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text(f"home = {base / 'bin'}\n")
    args, _ = Sandbox(network="open").wrap(["true"], interpreters=[venv])
    assert str(base) in binds(args, "--ro-bind") and str(site) in binds(args, "--tmpfs")
    assert args.index(str(site), args.index(str(base)) + 1) > args.index(str(base))      # emptied after it is bound


def test_a_hide_inside_a_framework_grant_shown_again_is_covered_after_it(tmp_path, monkeypatch):
    """A hermit installed in site-packages, covered without the framework; the method's grant (runtime/) is bound
    again; what sandbox.hide names inside that grant is covered after it."""
    fake_bwrap(monkeypatch)
    site = tmp_path / "site-packages"
    package = site / "hermit"
    (package / "runtime").mkdir(parents=True)
    (package / "runtime" / "secret.py").write_text("")
    (package / "sandbox_relay.py").write_text("")
    monkeypatch.setattr(sandbox_module, "PACKAGE", package)
    monkeypatch.setattr(sandbox_module, "RELAY", package / "sandbox_relay.py")
    monkeypatch.setattr(sandbox_module, "_python_paths", lambda packages: ([site], []))
    args, _ = Sandbox(network="isolated", framework=False, readable=[package / "runtime"],
                      hide=[package / "runtime" / "secret.py"]).wrap(["true"])
    covered = args.index(str(package), args.index("--tmpfs"))
    rebound = max(i for i, a in enumerate(args) if a == str(package / "runtime"))
    hidden = max(i for i, a in enumerate(args) if a == str(package / "runtime" / "secret.py"))
    assert covered < rebound < hidden and args[hidden - 2] == "--ro-bind"


def test_a_tool_results_keys_are_translated_too(tmp_path, monkeypatch):
    from hermit.sandbox import translate_values
    fake_bwrap(monkeypatch)
    box = Sandbox(network="isolated", binds=[Bind(tmp_path / "evidence", "/w/evidence")])
    out = translate_values(box, {str(tmp_path / "evidence" / "r.txt"): str(tmp_path / "evidence" / "s.txt")})
    assert out == {"/w/evidence/r.txt": "/w/evidence/s.txt"}


def test_a_sanitized_copy_reads_what_the_writer_left_unreadable(tmp_path):
    from hermit.sandbox import sanitized_copy
    src = tmp_path / "log"
    (src / "sealed").mkdir(parents=True)
    (src / "sealed" / "a.txt").write_text("a")
    (src / "b.txt").write_text("b")
    os.chmod(src / "b.txt", 0o200)
    os.chmod(src / "sealed", 0o300)
    sanitized_copy(src, tmp_path / "copy")
    assert (tmp_path / "copy" / "sealed" / "a.txt").read_text() == "a" and (tmp_path / "copy" / "b.txt").exists()


def test_a_module_imported_from_an_archive_cannot_be_hidden(tmp_path, monkeypatch):
    import zipfile
    from hermit.sandbox import hidden_paths
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("zpkg/__init__.py", "")
        z.writestr("zpkg/scoring.py", "X = 1\n")
    monkeypatch.syspath_prepend(str(archive))
    with pytest.raises(ValueError, match="from an archive"):
        hidden_paths(["zpkg.scoring"])


def test_a_sanitized_copy_reads_a_tree_sealed_from_its_root(tmp_path):
    from hermit.sandbox import sanitized_copy
    src = tmp_path / "log"
    (src / "inner").mkdir(parents=True)
    (src / "inner" / "a.txt").write_text("a")
    os.chmod(src / "inner" / "a.txt", 0o000)
    os.chmod(src / "inner", 0o000)
    os.chmod(src, 0o000)
    sanitized_copy(tmp_path, tmp_path / "copy", ["log"])
    assert (tmp_path / "copy" / "log" / "inner" / "a.txt").read_text() == "a"


def test_the_command_line_keeps_chdir_when_it_runs_unconfined(tmp_path, capfd):
    from hermit.sandbox import _main
    code = "import os; print(os.getcwd())"
    assert _main([json.dumps({"enabled": False, "chdir": str(tmp_path)}), "--", sys.executable, "-c", code]) == 0
    assert capfd.readouterr().out.strip() == str(tmp_path)


def test_an_editable_native_module_is_shown(tmp_path):
    """environment.packages names an editable distribution whose top-level module is an extension module."""
    import importlib.machinery
    from hermit.sandbox import _dist_sources
    checkout = tmp_path / "native-dist"
    checkout.mkdir()
    module = checkout / f"native{importlib.machinery.EXTENSION_SUFFIXES[0]}"
    module.write_bytes(b"")
    site = tmp_path / "site"
    site.mkdir()
    (site / "__editable__.native.pth").write_text(str(checkout) + "\n")

    class Dist:
        files = ["__editable__.native.pth"]
        metadata = {"Name": "native"}

        def locate_file(self, f):
            return site / f

        def read_text(self, name):
            return None
    assert _dist_sources(Dist()) == [module.resolve()]


def test_a_result_file_can_be_read_whole(tmp_path):
    from hermit.sandbox import read_regular
    big = tmp_path / "step_result.json"
    big.write_text(json.dumps({"extra": {"trace": "x" * (2 << 20)}}))
    assert len(read_regular(big)) == 1 << 20                       # the default cap
    assert json.loads(read_regular(big, limit=None))["extra"]["trace"] == "x" * (2 << 20)


def test_a_path_a_tool_returned_resolves_when_handed_to_another_tool(tmp_path, monkeypatch):
    """A result is spelled as the sandbox shows it (/w/adapter/...); an argument naming such a path reaches the
    tool as the host path it reads."""
    from types import SimpleNamespace
    from hermit.sandbox import inside_tools
    fake_bwrap(monkeypatch)
    adapter = tmp_path / "adapter"
    (adapter / "notes").mkdir(parents=True)
    (adapter / "notes" / "a.txt").write_text("found")
    box = Sandbox(network="isolated", layout=(tmp_path, "/w"))
    seen = {}
    tools = {"locate": SimpleNamespace(fn=lambda **a: {"path": str(adapter / "notes" / "a.txt")}),
             "read": SimpleNamespace(fn=lambda **a: (seen.update(a), Path(a["path"]).read_text())[1])}
    wrapped = inside_tools(tools, box)
    shown = wrapped["locate"].fn()["path"]
    assert shown == "/w/adapter/notes/a.txt"
    assert wrapped["read"].fn(path=shown, note="kept") == "found" and seen["note"] == "kept"
    wrapped["read"].fn(path=shown, pattern="/api//v1", files={shown: "utf8"})
    assert seen["pattern"] == "/api//v1"                        # not a name inside: left as given
    assert seen["files"] == {str(adapter / "notes" / "a.txt"): "utf8"}     # a key that names a path: back too
    box.close()


def test_an_editable_install_without_top_level_txt_shows_its_package_under_another_name(tmp_path):
    """A backend that writes no top_level.txt (Hatchling) and an import name unlike the distribution's: the
    packages and modules in the path its .pth adds are shown, not data beside them."""
    from hermit.sandbox import _dist_sources
    src = tmp_path / "checkout" / "src"
    (src / "mytool").mkdir(parents=True)
    (src / "mytool" / "__init__.py").write_text("")
    (src / "data").mkdir()                                       # no __init__.py: not a package
    site = tmp_path / "site"
    site.mkdir()
    (site / "_my_tool.pth").write_text(str(src) + "\n")

    class Dist:
        files = ["_my_tool.pth"]
        metadata = {"Name": "my-tool"}

        def locate_file(self, f):
            return site / f

        def read_text(self, name):
            return None
    assert _dist_sources(Dist()) == [(src / "mytool").resolve()]


def test_an_editable_namespace_package_without_top_level_txt_is_shown(tmp_path):
    from hermit.sandbox import _dist_sources
    src = tmp_path / "checkout" / "src"
    (src / "nspkg" / "sub").mkdir(parents=True)
    (src / "nspkg" / "sub" / "__init__.py").write_text("")       # nspkg/ itself has no __init__.py (PEP 420)
    (src / "assets").mkdir()
    (src / "assets" / "table.csv").write_text("a,b\n")          # no Python: not shown
    (src / "data").mkdir()                                       # a script beside answers (a flat layout's data/)
    (src / "data" / "prepare.py").write_text("")
    (src / "data" / "answers.jsonl").write_text("{}")
    site = tmp_path / "site"
    site.mkdir()
    (site / "_ns.pth").write_text(str(src) + "\n")

    class Dist:
        files = ["_ns.pth"]
        metadata = {"Name": "ns-dist"}

        def locate_file(self, f):
            return site / f

        def read_text(self, name):
            return None
    assert _dist_sources(Dist()) == [(src / "data" / "prepare.py").resolve(), (src / "nspkg" / "sub").resolve()]


def test_a_programs_own_installation_shows_no_python_packages_of_its_own(tmp_path, monkeypatch):
    """A CLI installed under ~/.local/bin is granted ~/.local (its installation): the site directories there (other
    interpreters' user packages) are emptied like any other interpreter's."""
    fake_bwrap(monkeypatch)
    local = tmp_path / ".local"
    (local / "bin").mkdir(parents=True)
    tool = local / "bin" / "sometool"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    site = local / "lib" / "python3.11" / "site-packages"
    (site / "foreign").mkdir(parents=True)
    monkeypatch.setenv("PATH", f"{local / 'bin'}:{os.environ['PATH']}")
    args, _ = Sandbox(network="open").wrap(["sometool"])
    assert str(local) in binds(args, "--ro-bind") and str(site) in binds(args, "--tmpfs")


@pytest.mark.skipif(not _bwrap_works(), reason="needs a working bubblewrap (unprivileged user namespaces)")
def test_python3_on_the_path_inside_is_the_frameworks_own(tmp_path):
    """Started from an unactivated venv, another python3 comes first on the host's PATH: inside, the framework's own
    interpreter does, with its packages."""
    other = tmp_path / "other"
    other.mkdir()
    (other / "python3").symlink_to("/usr/bin/python3")
    box = Sandbox(network="isolated")
    try:
        args, env = box.wrap(["/usr/bin/env", "python3", "-c", "import yaml, sys; print(sys.prefix)"],
                             env={**os.environ, "PATH": f"{other}:/usr/bin:/bin"}, readable=[other])
        proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert proc.stdout.strip() == sys.prefix
    finally:
        box.close()


def test_path_keeps_its_order_when_its_python3_is_the_frameworks_own(monkeypatch):
    """Only another interpreter first on PATH moves the framework's directory forward: tools keep their order."""
    fake_bwrap(monkeypatch)
    own = os.path.dirname(sys.executable)             # the framework's own directory, with python3 and python
    if not all(os.path.exists(os.path.join(own, name)) for name in ("python3", "python")):
        pytest.skip("the interpreter's directory lacks python3 or python")
    path = f"{own}:/usr/bin:/bin"
    box = Sandbox(network="isolated")
    try:
        _, env = box.wrap(["true"], env={**os.environ, "PATH": path})
        assert env["PATH"] == path
    finally:
        box.close()


def test_from_an_unactivated_venv_the_base_python3_first_on_path_is_not_its_own(tmp_path, monkeypatch):
    """A venv's python links to its base interpreter's file: the base's python3 first on PATH runs the base
    environment (emptied inside), so the venv's bin/ is moved forward."""
    fake_bwrap(monkeypatch)
    base, venv = tmp_path / "base" / "bin", tmp_path / "venv"
    base.mkdir(parents=True)
    (venv / "bin").mkdir(parents=True)
    real = os.path.realpath(sys.executable)
    for name in ("python3", "python"):
        (base / name).symlink_to(real)
    (venv / "bin" / "python").symlink_to(real)
    (venv / "pyvenv.cfg").write_text(f"home = {base}\n")
    monkeypatch.setattr(sys, "executable", str(venv / "bin" / "python"))
    monkeypatch.setattr(sys, "prefix", str(venv))
    box = Sandbox(network="isolated")
    try:
        _, env = box.wrap(["true"], env={**os.environ, "PATH": f"{base}:/usr/bin:/bin"})
        assert env["PATH"].split(os.pathsep)[:2] == [str(venv / "bin"), str(base)]
    finally:
        box.close()


def test_from_a_base_python_another_venv_first_on_path_is_not_its_own(tmp_path, monkeypatch):
    """The framework on its base Python: another venv whose python links to the same file runs that venv's
    environment, so the framework's directory is moved forward."""
    fake_bwrap(monkeypatch)
    real = os.path.realpath(sys.executable)
    venv = tmp_path / "other-venv"
    (venv / "bin").mkdir(parents=True)
    for name in ("python3", "python"):
        (venv / "bin" / name).symlink_to(real)
    (venv / "pyvenv.cfg").write_text(f"home = {os.path.dirname(real)}\n")
    monkeypatch.setattr(sys, "executable", real)
    monkeypatch.setattr(sys, "prefix", sys.base_prefix)
    box = Sandbox(network="isolated")
    try:
        _, env = box.wrap(["true"], env={**os.environ, "PATH": f"{venv / 'bin'}:/usr/bin:/bin"})
        assert env["PATH"].split(os.pathsep)[0] == os.path.dirname(real)
    finally:
        box.close()


def test_a_python_clis_own_installation_keeps_its_packages(tmp_path, monkeypatch):
    """A CLI that runs on Python (a pipx or venv install) imports from its own site directory: it is not emptied."""
    fake_bwrap(monkeypatch)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    tool = venv / "bin" / "sometool"
    tool.write_text(f"#!{venv / 'bin' / 'python'}\nimport sometool\n")
    tool.chmod(0o755)
    site = venv / "lib" / "python3.11" / "site-packages"
    (site / "sometool").mkdir(parents=True)
    monkeypatch.setenv("PATH", f"{venv / 'bin'}:{os.environ['PATH']}")
    args, _ = Sandbox(network="open").wrap(["sometool"])
    assert str(venv) in binds(args, "--ro-bind") and str(site) not in binds(args, "--tmpfs")


def test_a_module_also_in_a_later_archive_cannot_be_hidden(tmp_path, monkeypatch):
    import zipfile
    from hermit.sandbox import hidden_paths
    _fake_package(tmp_path / "dir")
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("pkg/__init__.py", "")
        z.writestr("pkg/scoring.py", "X = 1\n")
    monkeypatch.syspath_prepend(str(archive))
    monkeypatch.syspath_prepend(str(tmp_path / "dir"))           # the directory copy first, the archive later
    with pytest.raises(ValueError, match="from an archive"):
        hidden_paths(["pkg.scoring"])


def test_the_allowlist_proxy_parses_a_bracketed_ipv6_connect(monkeypatch):
    clean_proxy_env(monkeypatch)
    proxy = AllowlistProxy(["::1"])
    try:
        with socket.create_connection(("127.0.0.1", proxy.server.server_address[1])) as s:
            s.sendall(b"CONNECT [::1]:9 HTTP/1.1\r\nHost: [::1]:9\r\n\r\n")
            reply = s.recv(200)
        assert b"502" in reply or b"200" in reply                  # allowed and tried, not a parse failure
    finally:
        proxy.close()


def test_a_trusted_framework_interpreters_packages_stay_where_a_program_grant_holds_them(tmp_path, monkeypatch):
    """A benchmark's Python starts the adapter server on the framework's interpreter (HA_FRAMEWORK_PYTHON), named as
    trusted: its site-packages stay (the server imports mcp from there); another program's are emptied."""
    fake_bwrap(monkeypatch)
    fw = tmp_path / "fw"
    (fw / "bin").mkdir(parents=True)
    python = fw / "bin" / "python3"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    site = fw / "lib" / "python3.12" / "site-packages"
    (site / "mcp").mkdir(parents=True)
    trusted, _ = Sandbox(network="open").wrap([str(python)], interpreters=[fw])
    assert str(site) not in binds(trusted, "--tmpfs")
    plain, _ = Sandbox(network="open").wrap([str(python)])
    assert str(site) in binds(plain, "--tmpfs")


def test_unsafe_entries_are_dropped_under_a_read_only_directory(tmp_path):
    from hermit.sandbox import drop_unsafe
    out = tmp_path / "out"
    sealed = out / "sealed"
    sealed.mkdir(parents=True)
    (sealed / "leak").symlink_to("/etc/passwd")
    os.chmod(sealed, 0o555)
    try:
        assert drop_unsafe(out) == ["sealed/leak"] and not os.path.lexists(sealed / "leak")
    finally:
        os.chmod(sealed, 0o755)


def test_an_editable_native_module_under_another_name_is_shown(tmp_path):
    import importlib.machinery
    from hermit.sandbox import _dist_sources
    src = tmp_path / "build"
    src.mkdir()
    native = src / f"_native{importlib.machinery.EXTENSION_SUFFIXES[0]}"
    native.write_bytes(b"")
    site = tmp_path / "site"
    site.mkdir()
    (site / "_my_native.pth").write_text(str(src) + "\n")

    class Dist:
        files = ["_my_native.pth"]
        metadata = {"Name": "my-native"}

        def locate_file(self, f):
            return site / f

        def read_text(self, name):
            return None
    assert _dist_sources(Dist()) == [native.resolve()]


def test_bubblewrap_is_started_by_its_path(tmp_path, monkeypatch):
    """A bwrap installed outside the system directories (~/.local/bin on a cluster) is found through the caller's
    PATH: the command names it by path, so a child given PATH=/usr/bin:/bin (MWM's tool runner) still starts it."""
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name, *a, **k:
                        str(tmp_path / "bin" / "bwrap") if name == "bwrap" else REAL_WHICH(name, *a, **k))
    args, _ = Sandbox(network="open").wrap(["true"])
    assert args[0] == str(tmp_path / "bin" / "bwrap")
