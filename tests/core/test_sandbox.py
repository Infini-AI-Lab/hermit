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

import hermit.sandbox as sandbox_module
from hermit.sandbox import (FRAMEWORK, PACKAGE, RELAY, AllowlistProxy, Bind, Sandbox, network_mode,
                            package_closure)

REAL_WHICH = shutil.which


def fake_bwrap(monkeypatch):
    """Pretend bubblewrap is installed, to inspect the command without running it."""
    monkeypatch.setattr(sandbox_module.shutil, "which",
                        lambda name, *a, **k: "/usr/bin/bwrap" if name == "bwrap" else REAL_WHICH(name, *a, **k))


def binds(args, flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


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
    from hermit.sandbox import _interpreter
    for site in _interpreter()[1]:                                 # its site-packages emptied, then filled again
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
        assert res["import_yaml"] == ["ok", "yaml"]                # in the framework's dependency closure
        assert res["import_pytest"][0] == "error"                  # installed, but not shown
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
@pytest.mark.parametrize("cli", ["codex", "claude"])
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
    assert ("--bind", str(home.resolve()), "/w/agent_home") in named(args)
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
    fake_bwrap(monkeypatch)
    args, _ = Sandbox(network="isolated", framework=False).wrap(["true"])
    ro = binds(args, "--ro-bind")
    assert str(PACKAGE) not in ro and str(RELAY) in ro
    names = [Path(p).name for p in ro]
    assert not any(n.startswith("yaml") or n.startswith("mcp") for n in names)     # no framework dependency


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
