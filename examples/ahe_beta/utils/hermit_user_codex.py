"""Codex in a Harbor task container, with a simulated user talking to it while it works.

The interaction bridge lives in this example. Harbor imports this class through the run's own YAML:

    agent:
      name: harbor
      params:
        acting_agent:
          import_path: hermit_user_codex:HermitCodex   # the class name must be HermitCodex (see below)
          pythonpath: [../utils]                       # this file, user_hook.py and the user's own code
          kwargs:
            user:
              simulate: my_user:simulate               # module:function, importable from `pythonpath`
              params: {}                               # passed to the function as given
              wait_s: 3600                             # how long the agent waits for the user's next message
              exit_wait_s: 120                         # how long Codex gets to exit after the user ends
        environment:
          kwargs: {auto_mounting: true}                # required so the verifier can read host-written results

The function the task side writes:

    def simulate(task: dict, agent: Conversation, workspace: Workspace, params: dict) -> dict

    task       {"name": the Harbor task's name, "instruction": its instruction.md}
    agent      the running Codex session: write / poll / send / events / interrupt (class Conversation)
    workspace  the task container: run / read / write (class Workspace)
    returns    {"score": float | None, ...}: `score` becomes the trial's reward through the task's verifier
               (None: no reward is written); every other key is kept in user-verdict.json. A `files` key
               ({name: text}) writes more files beside it.

One `codex exec` runs for the whole conversation. Three hooks (user_hook.py, in the container) pass the messages
through a root-owned channel bridge. PostToolUse inserts a message while the agent works, PreToolUse denies tool
calls after an interrupt, Stop hands the reply to the user and continues the session with the user's next message.
The bridge keeps control messages and live evidence outside the Actor's writable workspace.

What the trial leaves in the agent's log directory (Hermit copies each into the learning material):

    user-verdict.json       what `simulate` returned (without `files`)
    user-transcript.jsonl   every user message, every reply, every tool call the user could see, with times
    user-reward.txt         the score, when there is one (the task's tests/test.sh copies it to the reward file)

The class is named HermitCodex because Hermit's runner (hermit/harbor/runner.py, `deploys_adapter`) gives the
adapter to an agent whose import path ends in `:HermitCodex`.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import inspect
import json
import math
import os
import shlex
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import processes
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harborside.codex import HermitCodex as _HermitCodex
from harborside.codex import HermitCodexOptions as _HermitCodexOptions
from pydantic import Field

CHANNEL_ROOT = "/run/ahe-user-channel"
CHANNEL_SOCKET = CHANNEL_ROOT + "/channel.sock"
CHANNEL_SERVER = "/usr/local/libexec/ahe-user-channel.py"
EVENTS = ("PreToolUse", "PostToolUse", "Stop")
FRAMES = {
    "reply_frame": "The user says:\n{text}",
    "insert_frame": "The user sent this while you were working:\n{text}",
    "interrupt_frame": "The user interrupted you: {text}\nStop here. Do not call any more tools. Say in one or "
    "two sentences where things stand, then wait for the user's next message.",
}


from channel import Conversation


class EnvironmentChannel:
    """Host-side view of the root-owned in-container user channel."""

    def __init__(
        self,
        environment: BaseEnvironment,
        loop: asyncio.AbstractEventLoop,
        host_events: Path,
    ):
        self.environment, self.loop, self.host_events = environment, loop, host_events
        self.closed = False

    def _run(self, command: str, timeout: float = 20.0) -> dict:
        result = asyncio.run_coroutine_threadsafe(
            self.environment.exec(command=command, user="root", timeout_sec=int(timeout)),
            self.loop,
        ).result(timeout + 10)
        if result.return_code:
            raise RuntimeError(result.stderr or result.stdout or "user channel command failed")
        try:
            value = json.loads(result.stdout or "{}")
        except ValueError as exc:
            raise RuntimeError("user channel returned invalid JSON") from exc
        return value if isinstance(value, dict) else {}

    def send(self, record: dict) -> None:
        if self.closed:
            raise RuntimeError("user channel is closed")
        encoded = base64.b64encode(
            json.dumps(record, ensure_ascii=False, default=str).encode()
        ).decode()
        self._run(
            shlex.join(
                [
                    "python3",
                    CHANNEL_SERVER,
                    "enqueue",
                    "--root",
                    CHANNEL_ROOT,
                    "--value",
                    encoded,
                ]
            )
        )

    async def asend(self, record: dict) -> None:
        if self.closed:
            raise RuntimeError("user channel is closed")
        encoded = base64.b64encode(
            json.dumps(record, ensure_ascii=False, default=str).encode()
        ).decode()
        result = await self.environment.exec(
            command=shlex.join(
                [
                    "python3",
                    CHANNEL_SERVER,
                    "enqueue",
                    "--root",
                    CHANNEL_ROOT,
                    "--value",
                    encoded,
                ]
            ),
            user="root",
            timeout_sec=20,
        )
        if result.return_code:
            raise RuntimeError(result.stderr or result.stdout or "user channel command failed")

    def read(self, since: int) -> tuple[int, list[dict]]:
        if self.closed:
            return since, []
        if not self.host_events.exists():
            if since:
                raise RuntimeError("user channel event mirror disappeared")
            return since, []
        events: list[dict] = []
        for line in self.host_events.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and int(value.get("seq", 0)) > since:
                events.append(value)
        next_seq = max([since, *(int(value.get("seq", since)) for value in events)])
        return next_seq, events

    async def aclose(self) -> None:
        if self.closed:
            return
        try:
            await self.environment.exec(
                command=(
                    "if test -f /run/ahe-services/ahe-user-channel.pid; then "
                    "kill $(cut -d: -f1 /run/ahe-services/ahe-user-channel.pid) "
                    "2>/dev/null || true; fi"
                ),
                user="root",
                timeout_sec=10,
            )
        finally:
            self.closed = True

    def close(self) -> None:
        if self.closed:
            return
        asyncio.run_coroutine_threadsafe(self.aclose(), self.loop).result(30)


class Workspace:
    """The task container, for the user's own checks. Called from the thread `simulate` runs in."""

    def __init__(
        self, environment: BaseEnvironment, loop: asyncio.AbstractEventLoop, stop
    ):
        self._environment, self._loop, self._stop = environment, loop, stop
        self._closed = False

    def quiesce(self) -> None:
        self._stop()

    def close(self) -> None:
        # Harbor owns container deletion. Closing only revokes the user's command capability.
        self._closed = True

    def _wait(self, coroutine, timeout: float | None):
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout)

    def run(self, command: str, timeout: float = 60.0, cwd: str | None = None) -> dict:
        if self._closed:
            raise RuntimeError("workspace access has ended")
        result = self._wait(
            self._environment.exec(command=command, cwd=cwd, timeout_sec=int(timeout)),
            timeout + 30,
        )
        return {
            "code": result.return_code,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
        }

    def read(self, path: str) -> str:
        out = self.run(f"cat {shlex.quote(path)}")
        if out["code"] != 0:
            raise FileNotFoundError(f"{path}: {out['stderr'].strip()}")
        return out["stdout"]

    def write(self, path: str, text: str) -> None:
        if self._closed:
            raise RuntimeError("workspace access has ended")
        with tempfile.TemporaryDirectory(prefix="hermit-user-") as tmp:
            local = Path(tmp) / PurePosixPath(path).name
            local.write_text(text)
            self._wait(self._environment.upload_file(local, path), 120)

    def service_identity(self, name):
        return self._environment.environment_name + ":" + name

    def service_run(self, command, *, service, timeout, cwd):
        if self._closed:
            raise RuntimeError("workspace access has ended")
        result = self._wait(
            self._environment.service_exec(
                command, service=service, cwd=cwd, timeout_sec=int(timeout)
            ),
            timeout + 30,
        )
        return {
            "code": result.return_code,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
        }

    def service_read(self, path, *, service, max_bytes):
        # Copy an explicitly declared controller-owned output; reject non-regular files first.
        quoted = shlex.quote(path)
        check = self.service_run(
            f"test -f {quoted} && test ! -L {quoted} && stat -c %s {quoted}",
            service=service,
            timeout=30,
            cwd="/",
        )
        if check["code"]:
            raise FileNotFoundError(path)
        if int(check["stdout"].strip()) > max_bytes:
            raise OverflowError(path)
        with tempfile.TemporaryDirectory(prefix="ahe-service-") as tmp:
            target = Path(tmp) / "artifact"
            self._wait(
                self._environment.service_download_file(path, target, service=service),
                120,
            )
            if (
                target.is_symlink()
                or not target.is_file()
                or target.stat().st_size > max_bytes
            ):
                raise ValueError("invalid or oversized service artifact")
            return target.read_bytes()


def load(spec: str):
    module, _, name = str(spec).partition(":")
    if not module or not name:
        raise ValueError(f"user.simulate must be module:function, not {spec!r}")
    return getattr(importlib.import_module(module), name)


class HermitCodexOptions(_HermitCodexOptions):
    user: dict | None = Field(
        default=None,
        description="The simulated user: {simulate: module:function, params: {}, "
        "wait_s, exit_wait_s}. Omit for a session without a user.",
    )


class HermitCodex(_HermitCodex):
    options_model = HermitCodexOptions
    options: HermitCodexOptions

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._user_summary: dict | None = None
        if self.options.user:
            load(
                self.options.user["simulate"]
            )  # a wrong name fails before any container
            self.options.dangerously_bypass_hook_trust = (
                True  # the hooks are this file's own
            )

    # ----- the hooks ---------------------------------------------------------------------------------------------------

    async def _install_root_file(
        self, environment: BaseEnvironment, local: Path, remote_path: str, *, mode: str
    ) -> None:
        temporary = f"/tmp/.ahe-channel-upload-{uuid.uuid4().hex}"
        await environment.upload_file(local, temporary)
        await self.exec_as_root(
            environment,
            command=(
                f"install -m {shlex.quote(mode)} {shlex.quote(temporary)} "
                f"{shlex.quote(remote_path)} && rm -f {shlex.quote(temporary)}"
            ),
        )

    async def _install_root_text(
        self, environment: BaseEnvironment, text: str, remote_path: str, *, mode: str
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="hermit-root-file-") as tmp:
            local = Path(tmp) / PurePosixPath(remote_path).name
            local.write_text(text)
            await self._install_root_file(environment, local, remote_path, mode=mode)

    async def _upload_effective_config(
        self, environment: BaseEnvironment, config: dict[str, Any], remote_path: str
    ) -> None:
        """After Hermit's deployment: the adapter's hooks stay, except its Stop hooks, which user_hook.py runs itself
        before the user hears a reply (Codex runs the hooks of one event at the same time)."""
        await super()._upload_effective_config(environment, config, remote_path)
        spec = self.options.user
        if not spec:
            return
        if self._agent_identity is None:
            await self._ensure_agent_owned_dir(environment, self._REMOTE_CODEX_HOME)
        if not await self._ensure_python3(environment):
            raise RuntimeError("the user channel needs python3 in the task's image")
        workdir = await self._workdir(environment)
        hook_dir = PurePosixPath(CHANNEL_ROOT)
        script = (hook_dir / "user_hook.py").as_posix()
        adapter = (
            json.loads(self._hooks_file.read_text()).get("hooks", {})
            if self._hooks_file
            else {}
        )
        for group in adapter.get("Stop", []):
            if group.get("matcher") or any(
                h.get("type") != "command" for h in group.get("hooks", [])
            ):
                raise ValueError(
                    "interactive Stop hooks require unconditional command hooks"
                )
        settings = {
            "channel": CHANNEL_SOCKET,
            "wait_s": float(spec.get("wait_s") or 3600),
            "adapter_stop": [
                h
                for group in adapter.get("Stop") or []
                for h in group.get("hooks") or []
                if h.get("type") == "command"
            ],
            **{key: str(spec.get(key) or default) for key, default in FRAMES.items()},
        }
        timeout = int(
            settings["wait_s"]
            + sum(float(h.get("timeout") or 60) for h in settings["adapter_stop"])
            + 60
        )
        merged = {
            event: list(groups) for event, groups in adapter.items() if event != "Stop"
        }
        for event in EVENTS:
            merged.setdefault(event, []).append(
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"python3 -B {shlex.quote(script)} {event}",
                            "timeout": timeout if event == "Stop" else 30,
                        }
                    ]
                }
            )
        remote_hooks = workdir.rstrip("/") + "/.codex/hooks.json"
        await self.exec_as_root(
            environment,
            command=f"mkdir -p {shlex.quote(workdir.rstrip('/') + '/.codex')}",
        )
        await self._install_root_file(
            environment,
            Path(__file__).with_name("user_hook.py"),
            script,
            mode="0555",
        )
        await self._install_root_text(
            environment,
            json.dumps(settings, indent=1),
            (hook_dir / "user_hook.json").as_posix(),
            mode="0444",
        )
        await self._upload_text(
            environment, json.dumps({"hooks": merged}, indent=2), remote_hooks
        )
        codex_dir = workdir.rstrip("/") + "/.codex"
        await self.exec_as_root(
            environment,
            command=(
                # The hook configuration must not be removable by Actor
                # commands through the writable project directory.
                f"chown -R root:root {shlex.quote(codex_dir)} && "
                f"chmod -R a+rX,go-w {shlex.quote(codex_dir)} && "
                f"chown root:root {shlex.quote(remote_hooks)} && "
                f"chmod 0444 {shlex.quote(remote_hooks)}"
            ),
        )
        self._hooks_active = True

    async def _start_user_channel(
        self, environment: BaseEnvironment
    ) -> EnvironmentChannel:
        """Start the root-owned bridge before the Actor and its process boundary."""
        with tempfile.TemporaryDirectory(prefix="ahe-channel-") as tmp:
            local = Path(tmp) / "ahe-user-channel.py"
            local.write_bytes(Path(__file__).with_name("channel_server.py").read_bytes())
            remote = "/tmp/ahe-user-channel.py"
            await environment.upload_file(local, remote)
        try:
            await self.exec_as_root(
                environment,
                command=(
                    f"mkdir -p {shlex.quote(CHANNEL_ROOT)} /run/ahe-services /usr/local/libexec && "
                    "chmod 0700 /run/ahe-services && "
                    f"install -m 0755 {shlex.quote(remote)} {shlex.quote(CHANNEL_SERVER)} && "
                    f"rm -f {shlex.quote(remote)} && "
                    f"rm -f {shlex.quote(CHANNEL_ROOT + '/STOP')} "
                    f"{shlex.quote(CHANNEL_ROOT + '/channel.sock')} "
                    f"{shlex.quote('/run/ahe-services/ahe-user-channel.pid')} && "
                    f"nohup python3 -B {shlex.quote(CHANNEL_SERVER)} serve "
                    f"--root {shlex.quote(CHANNEL_ROOT)} "
                    f"--pid-file /run/ahe-services/ahe-user-channel.pid "
                    "--mirror /logs/agent/.ahe-channel-events.jsonl "
                    f">{shlex.quote(CHANNEL_ROOT + '/server.log')} 2>&1 </dev/null & "
                    "for i in $(seq 1 50); do "
                    f"test -S {shlex.quote(CHANNEL_SOCKET)} && exit 0; sleep 0.1; "
                    "done; echo 'user channel did not start' >&2; exit 1"
                ),
            )
        except BaseException:
            await environment.exec(
                command=(
                    "test -f /run/ahe-services/ahe-user-channel.pid && "
                    "kill $(cut -d: -f1 /run/ahe-services/ahe-user-channel.pid) 2>/dev/null || true"
                ),
                user="root",
                timeout_sec=10,
            )
            raise
        return EnvironmentChannel(
            environment,
            asyncio.get_running_loop(),
            self.logs_dir / ".ahe-channel-events.jsonl",
        )

    # ----- the session -------------------------------------------------------------------------------------------------

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        spec = self.options.user
        if not spec:
            return await super().run(instruction, environment, context)
        if not environment.is_path_mounted(self.environment_logs_dir, self.logs_dir):
            raise RuntimeError(
                "simulated-user runs require auto_mounting so the verifier can read "
                "the host-written reward and evidence; live user control uses the "
                "root-owned channel bridge"
            )
        loop = asyncio.get_running_loop()
        first: dict = {}
        started = asyncio.Event()
        codex: asyncio.Task | None = None
        channel = await self._start_user_channel(environment)

        def start(text: str) -> None:
            first["text"] = text
            loop.call_soon_threadsafe(started.set)

        baseline_script = (
            inspect.getsource(processes)
            + "\nimport json; print(json.dumps(snapshot()))"
        )
        baseline_run = await self.exec_as_root(
            environment, command=shlex.join(["python3", "-c", baseline_script])
        )
        if baseline_run.return_code:
            await channel.aclose()
            raise RuntimeError("could not capture rollout process boundary")
        baseline = json.loads(baseline_run.stdout)
        stopped = False
        stopping = asyncio.Lock()

        async def stop_actor():
            nonlocal stopped
            async with stopping:
                if stopped:
                    return
                await conversation.end_async()
                if codex is not None:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(codex),
                            timeout=float(spec.get("exit_wait_s", 10)),
                        )
                    except TimeoutError:
                        # User abandonment is allowed, but all children must stop before snapshotting.
                        codex.cancel()
                    if codex.cancelled() or codex.cancelling():
                        await asyncio.gather(codex, return_exceptions=True)
                cleanup = (
                    inspect.getsource(processes)
                    + "\nquiesce("
                    + repr(baseline)
                    + ")"
                )
                result = await self.exec_as_root(
                    environment,
                    command=shlex.join(["python3", "-c", cleanup]),
                    timeout_sec=15,
                )
                if result.return_code:
                    raise RuntimeError(
                        "Actor processes could not be quiesced: "
                        + str(result.stderr)
                    )
                stopped = True

        def stop_sync():
            asyncio.run_coroutine_threadsafe(stop_actor(), loop).result(
                float(spec.get("exit_wait_s", 10)) + 45
            )

        conversation = Conversation(
            None,
            start,
            lambda: codex is None or not codex.done(),
            stop_sync,
            transport=channel,
        )
        task = {"name": environment.environment_name, "instruction": instruction}
        workspace = Workspace(environment, loop, stop_sync)
        user_params = dict(spec.get("params") or {})
        user_params["artifact_root"] = str(self.logs_dir / "ahe-session")
        os.environ["AHE_BETA_USAGE_PATH"] = str(
            self.logs_dir / "ahe-session" / "usage.jsonl"
        )
        simulate = loop.run_in_executor(
            None, load(spec["simulate"]), task, conversation, workspace, user_params
        )
        waiter = asyncio.ensure_future(started.wait())
        verdict = None
        error = None
        try:
            await asyncio.wait({simulate, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if started.is_set() and not stopped:
                codex = asyncio.ensure_future(
                    super().run(first["text"], environment, context)
                )
            verdict = await simulate
            if not isinstance(verdict, dict):
                raise TypeError(
                    f"simulate returned {type(verdict).__name__}, not a dict"
                )
            await stop_actor()  # Actor failures cannot become a successful user score.
        except BaseException as exc:
            error = exc
            raise
        finally:
            waiter.cancel()
            await conversation.end_async()
            try:
                await stop_actor()
            except BaseException as cleanup_error:
                if error is None:
                    error = cleanup_error
                    raise
            finally:
                workspace.close()
                conversation.events()
                try:
                    await channel.aclose()
                finally:
                    self._leave(verdict, conversation, error)

    def _leave(
        self,
        verdict: dict | None,
        conversation: Conversation,
        error: BaseException | None,
    ) -> None:
        """The user's files, at the top of the agent's log directory (Hermit's log curation copies top-level files)."""
        kept = {k: v for k, v in (verdict or {}).items() if k != "files"}
        if error is not None:
            kept["error"] = f"{type(error).__name__}: {error}"
        said = [
            e
            for e in conversation.transcript
            if e["from"] == "user" and e["kind"] == "message"
        ]
        self._user_summary = {
            "score": kept.get("score"),
            "rating": kept.get("rating"),
            "outcome": kept.get("outcome"),
            "messages": len(said),
            "interrupts": sum(
                1
                for e in conversation.transcript
                if e["from"] == "user" and e["kind"] == "interrupt"
            ),
            "inserted": sum(
                1
                for e in conversation.transcript
                if e.get("kind") == "delivered" and e.get("at") == "PostToolUse"
            ),
            "replies": sum(
                1 for e in conversation.transcript if e.get("kind") == "reply"
            ),
            "error": kept.get("error"),
        }
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._write_host_text(
            self.logs_dir / "user-verdict.json",
            json.dumps(kept, indent=1, ensure_ascii=False, default=str) + "\n",
        )
        self._write_host_text(
            self.logs_dir / "user-transcript.jsonl",
            "".join(
                json.dumps(entry, ensure_ascii=False, default=str) + "\n"
                for entry in conversation.transcript
            ),
        )
        (self.logs_dir / ".ahe-channel-events.jsonl").unlink(missing_ok=True)
        reward = self.logs_dir / "user-reward.txt"
        reward.unlink(missing_ok=True)
        for name, text in ((verdict or {}).get("files") or {}).items():
            if Path(str(name)).name != name or name in {
                "reward.txt",
                "verdict.json",
                "transcript.jsonl",
            }:
                raise ValueError(
                    "user attachment name is reserved or contains a directory"
                )
            self._write_host_text(self.logs_dir / f"user-{name}", str(text))
        if kept.get("score") is not None and error is None:
            score = float(kept["score"])
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("user score must be finite and between 0 and 1")
            self._write_host_text(reward, f"{score:g}\n")
        for path in self.logs_dir.glob("user-*"):
            if path.is_file() and not path.is_symlink():
                os.chmod(path, 0o644)

    @staticmethod
    def _write_host_text(path: Path, text: str) -> None:
        """Write final host evidence without following an Actor-created symlink."""
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o644)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(text)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        if self._user_summary is not None:
            context.metadata = {**(context.metadata or {}), "user": self._user_summary}
