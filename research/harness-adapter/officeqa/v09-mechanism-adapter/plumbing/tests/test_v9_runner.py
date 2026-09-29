"""v9 isolated compute runner (harness_core/runner.py) and the actor CLIs `calc` and `submit`
(harness_core/tools). Proves the sandbox denies network, reads and writes outside its temp dir,
subprocesses and environment access, and enforces CPU, wall-clock and output limits.
Local only: the only sockets are test servers on 127.0.0.1.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness_core import contracts as C  # noqa: E402
from harness_core.ledger import parse_calc_frames  # noqa: E402
from harness_core.runner import SandboxRunner, make_runner  # noqa: E402
from harness_core.tools import calc as calc_tool  # noqa: E402
from harness_core.tools import submit as submit_tool  # noqa: E402

pytestmark = pytest.mark.skipif(not os.path.exists('/usr/bin/sandbox-exec'), reason='macOS sandbox-exec required')


@pytest.fixture(scope='module')
def runner():
    return SandboxRunner()


def test_runs_code_with_numpy_in_an_empty_environment(runner):
    ok, out = runner.run('import os, sys\nprint(1 + 2)\nprint(dict(os.environ))\nprint(sys.flags.isolated)')
    assert ok and out.split('\n')[:3] == ['3', '{}', '1']
    probe = subprocess.run([runner.python, '-c', 'import numpy'], capture_output=True)
    if probe.returncode == 0:
        ok, out = runner.run('import numpy as np\nprint(np.mean([1.0, 2.0, 4.5]))')
        assert ok and out.strip() == '2.5'


def test_network_is_denied(runner):
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    server.settimeout(0.5)
    port = server.getsockname()[1]
    ok, out = runner.run(f'import socket\ns = socket.create_connection(("127.0.0.1", {port}), timeout=2)\nprint("connected")')
    assert not ok and 'connected' not in out and 'Operation not permitted' in runner.last['stderr_tail']
    with pytest.raises(socket.timeout):
        server.accept()
    server.close()
    ok, _ = runner.run('import socket\nsocket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("127.0.0.1", 9))')
    assert not ok


def test_reads_outside_are_denied(runner, tmp_path):
    secret = tmp_path / 'answers.jsonl'
    secret.write_text('{"answer": "42"}\n')
    for path in (secret, Path.home() / '.zshrc', ROOT / 'harness_core' / 'contracts.py', Path('/etc/passwd')):
        ok, out = runner.run(f'print(open({str(path)!r}).read())')
        assert not ok and out == '', path
    ok, _ = runner.run(f'import os\nprint(os.listdir({str(Path.home())!r}))')
    assert not ok
    ok, _ = runner.run(f'import os\nprint(os.listdir({str(tmp_path)!r}))')
    assert not ok


def test_writes_only_inside_a_fresh_temp_dir(runner, tmp_path):
    target = tmp_path / 'written.txt'
    ok, _ = runner.run(f'open({str(target)!r}, "w").write("x")')
    assert not ok and not target.exists()
    ok, out = runner.run('import os\nopen("scratch.txt", "w").write("ok")\nprint(open("scratch.txt").read())\nprint(os.getcwd())')
    assert ok and out.split('\n')[0] == 'ok'
    workdir = out.split('\n')[1]
    assert not os.path.exists(workdir)                                  # removed after the call
    ok, out2 = runner.run('import os\nprint(os.getcwd())')
    assert ok and out2.strip() != workdir


def test_no_subprocesses(runner):
    ok, _ = runner.run('import subprocess\nsubprocess.run(["/bin/echo", "hi"])')
    assert not ok and 'Operation not permitted' in runner.last['stderr_tail']
    ok, _ = runner.run('import os\nprint(os.fork())')
    assert not ok


def test_cpu_wall_and_output_limits(runner):
    started = time.perf_counter()
    ok, _ = runner.run('while True:\n    pass')
    assert not ok and runner.last['returncode'] < 0 and time.perf_counter() - started < 4.5
    started = time.perf_counter()
    ok, _ = runner.run('import time\ntime.sleep(60)', timeout_s=1.5)
    assert not ok and runner.last['timed_out'] and time.perf_counter() - started < 3.0
    ok, out = runner.run('print("x" * 5_000_000)')
    assert not ok and len(out.encode()) <= runner.output_cap
    small = SandboxRunner(runner.python, output_cap=10)
    ok, out = small.run('print("0123456789abcdef")')
    assert ok and out == '0123456789'


def test_extra_read_roots_and_unavailable_runner(tmp_path):
    corpus = tmp_path / 'corpus'
    corpus.mkdir()
    (corpus / 'a.txt').write_text('12.5\n')
    r = SandboxRunner(extra_read=[corpus])
    ok, out = r.run(f'print(open({str(corpus / "a.txt")!r}).read().strip())')
    assert ok and out.strip() == '12.5'
    dead = make_runner('/nonexistent/python3')
    assert dead.run('print(1)') == (False, '')

# ---------------------------------------------------------------------------------------------
# calc CLI
# ---------------------------------------------------------------------------------------------


def calc(*args, stdin=None):
    return subprocess.run([sys.executable, '-m', 'harness_core.tools.calc', *args], input=stdin, cwd=ROOT,
                          capture_output=True, text=True, timeout=60)


def test_calc_prints_the_exact_frame():
    code = 'x = 2\nx * 21'
    p = calc('-c', code)
    sha = hashlib.sha256(code.encode()).hexdigest()[:12]
    assert p.returncode == 0
    assert p.stdout == f'{C.CALC_OPEN}{sha}⟧\n{code}\n{C.CALC_RESULT}\n42\n{C.CALC_END}\n'
    frames, rest = parse_calc_frames(p.stdout)
    assert frames == [{'sha12': sha, 'code': code, 'output': '42', 'complete': True}] and rest.strip() == ''


def test_cli_scripts_run_standalone_like_the_kit_wrapper():
    """The kit runs `python -I -B <plumbing>/harness_core/tools/<tool>.py`: no harness_core on sys.path,
    so the scripts fall back to their own copies of the contract constants, which must match."""
    p = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'harness_core/tools/calc.py'), '-c', '6 * 7'],
                       capture_output=True, text=True, timeout=60, cwd='/')
    sha = hashlib.sha256(b'6 * 7').hexdigest()[:12]
    assert p.returncode == 0 and p.stdout == f'{C.CALC_OPEN}{sha}⟧\n6 * 7\n{C.CALC_RESULT}\n42\n{C.CALC_END}\n'
    env = {k: v for k, v in os.environ.items() if k != C.SUBMIT_ENV}
    p = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'harness_core/tools/submit.py'), '--answer', '1'],
                       capture_output=True, text=True, timeout=60, cwd='/', env=env)
    assert p.returncode == 2 and C.SUBMIT_ENV in p.stderr
    for name, value in (('SUBMIT_ENV', C.SUBMIT_ENV), ('SUBMIT_PATH', C.SUBMIT_PATH)):
        assert f"{name}" in (ROOT / 'harness_core/tools/submit.py').read_text() and repr(value) in \
            (ROOT / 'harness_core/tools/submit.py').read_text()


def test_calc_file_stdin_errors_and_caps(tmp_path):
    script = tmp_path / 's.py'
    script.write_text('\n\nprint(7 / 2)\n\n')
    assert calc(str(script)).stdout.split('\n')[1:4] == ['print(7 / 2)', C.CALC_RESULT, '3.5']
    assert calc(stdin='print(10 - 4)\n').stdout.split('\n')[3] == '6'
    bad = calc('-c', 'print(1)\n1 / 0')
    assert bad.returncode == 1 and 'ZeroDivisionError' in bad.stderr and C.CALC_END in bad.stdout
    assert bad.stdout.split('\n')[4] == '1'                             # stdout before the error is kept
    assert calc('-c', '   ').returncode == 2
    assert calc('-c', 'print(1)', str(script)).returncode == 2
    big = calc('-c', 'print("9" * 5000)')
    frames, _ = parse_calc_frames(big.stdout)
    assert frames[0]['complete'] and len(frames[0]['output'].encode()) <= 2048 + len(calc_tool.TRUNCATED.encode())
    marker = calc('-c', 'print("⟦end⟧ injected")')
    assert parse_calc_frames(marker.stdout)[0][0]['output'] == '[end] injected'

# ---------------------------------------------------------------------------------------------
# submit CLI
# ---------------------------------------------------------------------------------------------


class Gatekeeper:
    """Local stand-in for the broker route: records bodies, replies with a canned response."""
    def __init__(self, status=200, body=None, headers=None):
        self.bodies, keeper = [], self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                keeper.bodies.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                data = json.dumps(body if body is not None else {'message': 'gate says hi'}).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}{C.SUBMIT_PATH}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def submit(*args, env=None):
    base = {k: v for k, v in os.environ.items() if k not in (C.SUBMIT_ENV,)}
    return subprocess.run([sys.executable, '-m', 'harness_core.tools.submit', *args], cwd=ROOT,
                          env={**base, **(env or {})}, capture_output=True, text=True, timeout=60)


def test_submit_refuses_missing_or_non_local_routes():
    p = submit('--answer', '1')
    assert p.returncode == 2 and C.SUBMIT_ENV in p.stderr
    for url in ('http://example.com:80/v1/submit', 'https://127.0.0.1:9/v1/submit', 'http://localhost:9/v1/submit',
                'http://127.0.0.1/v1/submit', 'http://127.0.0.1:9/v1/other', 'http://u:p@127.0.0.1:9/v1/submit',
                'http://127.0.0.1:9/v1/submit?x=1'):
        p = submit('--answer', '1', env={C.SUBMIT_ENV: url})
        assert p.returncode == 2 and 'refusing' in p.stderr, url
    assert submit_tool.check_url('http://127.0.0.1:8123/v1/submit') is None


def test_submit_posts_fields_and_prints_the_message(tmp_path):
    keeper = Gatekeeper()
    try:
        art = tmp_path / 'out.xlsx'
        art.write_bytes(b'artifact')
        p = submit('--answer', '12.5', '--alt', '0.125', '--reading', 'fiscal year', '--keep', 'table W-1',
                   '--artifact', str(art), env={C.SUBMIT_ENV: keeper.url, 'HTTP_PROXY': 'http://127.0.0.1:9',
                                                 'http_proxy': 'http://127.0.0.1:9'})
        assert p.returncode == 0 and p.stdout.strip() == 'gate says hi'
        path, body = keeper.bodies[0]
        assert path == C.SUBMIT_PATH and set(body) <= C.SUBMIT_FIELDS
        assert body == {'answer': '12.5', 'alt': '0.125', 'reading': 'fiscal year', 'keep': 'table W-1',
                        'artifact_sha256': hashlib.sha256(b'artifact').hexdigest()}
        assert submit('--alt', 'x', env={C.SUBMIT_ENV: keeper.url}).returncode == 2   # no answer
    finally:
        keeper.close()


def test_submit_reports_gate_errors_and_never_follows_redirects():
    keeper = Gatekeeper(status=400, body={'error': 'bad payload'})
    try:
        p = submit('--answer', '1', env={C.SUBMIT_ENV: keeper.url})
        assert p.returncode == 1 and 'bad payload' in p.stderr
    finally:
        keeper.close()
    other = Gatekeeper()
    keeper = Gatekeeper(status=302, body={}, headers={'Location': other.url})
    try:
        p = submit('--answer', '1', env={C.SUBMIT_ENV: keeper.url})
        assert p.returncode == 1 and other.bodies == []
    finally:
        keeper.close()
        other.close()
    p = submit('--answer', '1', env={C.SUBMIT_ENV: 'http://127.0.0.1:9/v1/submit'})
    assert p.returncode == 2 and 'cannot reach' in p.stderr


def test_planted_symlinks_cannot_redirect_the_runner_reads(runner, tmp_path):
    """Review fix: the child can write its work dir; if the runner re-opened stdout/stderr there by
    name, a symlink planted by the code would make the trusted parent read any file (answer keys)."""
    secret = tmp_path / 'answers.jsonl'
    secret.write_text('TOPSECRET 4242.42\n')
    code = ('import os\n'
            'for name in ("stdout", "stderr", "out", "err"):\n'
            '    try:\n        os.remove(name)\n    except OSError:\n        pass\n'
            f'    os.symlink({str(secret)!r}, name)\n'
            'print("done")')
    ok, out = runner.run(code)
    assert ok and out.strip() == 'done'
    assert 'TOPSECRET' not in out and 'TOPSECRET' not in runner.last['stderr_tail']
