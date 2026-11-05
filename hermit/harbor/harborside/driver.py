"""One multi-step Harbor task, run a step at a time on request: a stream's environment that lives between the
Trainer's steps.

    python -m harborside.driver --task-dir <task> --stream-dir <dir> --name <trial name> --socket <path>
        --agent-json '{"import_path": ..., "model_name": ..., "kwargs": {...}}' --environment-json '{...}'
        [--verifier-json '{...}'] [--idle-s 14400]

Harbor's own `MultiStepTrial` runs every step of a task in one go. This subclass keeps its lifecycle (the
environment, the agent's install, each step's setup, agent phase, verifier and archive, the trial's result at the
end) and hands the step loop to whoever connects to the socket: a request names the next step, the driver runs it
and answers with the step's result; `stop` ends the trial (the aggregate reward, the environment down). Between
requests the environment waits; after `--idle-s` without one it stops by itself. Steps run in order; a request for
another step is refused with the index the driver is at, so concurrent rollouts of one stream queue up outside.

The driver writes `driver.json` (pid, socket, trial dir) in the stream directory once it listens and logs to its
stderr (the client sends that to driver.log); Harbor's trial log, each step's agent/ and verifier/ and the final
result.json are under the trial directory. Each step's result is also kept as `replies/<index>.json`, for a client
that asked, lost the connection and asks again (`result`).

Protocol: one JSON object per line, one request per connection (see hermit/harbor/stream.py).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import time
import traceback
from uuid import uuid4

from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig, VerifierConfig
from harbor.models.trial.result import ExceptionInfo, StepResult
from harbor.trial.multi_step import MultiStepTrial


def log(message: str) -> None:
    print(f"[driver {time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


class StepDriverTrial(MultiStepTrial):
    """MultiStepTrial whose step loop is fed by requests."""

    @classmethod
    async def create(cls, config: TrialConfig) -> "StepDriverTrial":
        cls._resolve_agent_skills(config)
        task, download = await cls._load_task(config)
        if not task.has_steps:
            raise ValueError(f"{task.name} has no [[steps]]: a single-step task is a trial, not a stream")
        return cls(config, _task=task, _task_download_result=download)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commands: asyncio.Queue = asyncio.Queue()
        self.prepared = asyncio.Event()
        self.done = asyncio.Event()                        # trial.run() returned: result.json written, environment down
        self.error: str | None = None
        self.running: str | None = None
        self.next_index = 1
        self.aborted: str | None = None
        self.loop_done = False                             # the step loop ended; the environment may still be stopping
        self.finished = False                              # the environment is down
        self.replies_dir = Path(self.config.trials_dir).parent / "replies"
        self.replies_dir.mkdir(parents=True, exist_ok=True)

    # --- lifecycle ---------------------------------------------------------------------------------------------
    async def _prepare(self) -> None:
        try:
            await super()._prepare()
        except BaseException as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.prepared.set()

    async def _run(self) -> None:
        self.result.step_results = []
        steps = {s.name: s for s in self.task.config.steps or []}
        total = len(steps)
        while True:
            command, future = await self.commands.get()
            if command.get("op") == "stop":
                self._resolve(future, {"ok": True, "stopped": True})
                break
            step = steps[command["name"]]
            index = int(command["index"])
            self.running = step.name
            step_result = StepResult(step_name=step.name)
            self.result.step_results.append(step_result)
            try:
                self._apply(command)
                await self._run_step(step, step_result, index=index, total=total)
            except Exception as exc:                        # the step's own errors are recorded inside _run_step
                if step_result.exception_info is None:
                    step_result.exception_info = ExceptionInfo.from_exception(exc)
                log(f"step {step.name}: {type(exc).__name__}: {exc}")
            stop = self._should_stop_after_step(step, step_result)
            if stop:
                self._move_agent_dir_to_step(step)
                self.aborted = (f"step {step.name} failed" if step_result.exception_info and not step_result.verifier_result
                                else f"step {step.name} under min_reward")
            self.running = None
            self.next_index = index + 1
            reply = {"ok": True, "index": index, "step": step_result.model_dump(mode="json"),
                     "paths": {"trial_dir": str(self.paths.trial_dir), "step_dir": str(self.paths.step_dir(step.name))},
                     "aborted": self.aborted, "last": index >= total}
            (self.replies_dir / f"{index}.json").write_text(json.dumps(reply, default=str))
            self._resolve(future, reply)
            if stop or index >= total:
                break
        self.loop_done = True
        self._drain({"ok": True, "stopped": True})
        self.result.verifier_result = self._select_multi_step_reward()
        try:
            await self._stop_agent_environment()
            self.paths.cleanup_empty_mount_dirs()
        finally:
            self.finished = True

    def _drain(self, reply: dict) -> None:
        """Answer whatever was queued after the step loop ended."""
        while not self.commands.empty():
            _, future = self.commands.get_nowait()
            self._resolve(future, reply)

    def _apply(self, command: dict) -> None:
        """What a step changes on the agent: the deployment it runs with."""
        if hasattr(self.agent, "set_deploy_dir"):
            self.agent.set_deploy_dir(command.get("deploy_dir"))
        elif command.get("deploy_dir"):
            log(f"the agent {type(self.agent).__name__} takes no deploy_dir: step {command['name']} runs without the adapter")

    @staticmethod
    def _resolve(future: asyncio.Future, value: dict) -> None:
        if not future.done():
            future.set_result(value)

    # --- requests ----------------------------------------------------------------------------------------------
    def status(self) -> dict:
        return {"ok": True, "ready": self.prepared.is_set() and self.error is None, "error": self.error,
                "next_index": self.next_index, "running": self.running, "finished": self.finished,
                "aborted": self.aborted, "trial_dir": str(self.paths.trial_dir), "pid": os.getpid()}

    async def handle(self, request: dict) -> dict:
        op = request.get("op")
        if op == "status":
            return self.status()
        if op == "result":
            path = self.replies_dir / f"{int(request.get('index', 0))}.json"
            if path.is_file():
                return json.loads(path.read_text())
            return {"ok": False, "error": f"no result for step {request.get('index')}"}
        if op == "stop":
            if not (self.loop_done or self.error):
                future: asyncio.Future = asyncio.get_running_loop().create_future()
                await self.commands.put(({"op": "stop"}, future))
                await future
            # Answer once the environment is down and the trial's result.json is written (serve() sets `done`).
            try:
                await asyncio.wait_for(self.done.wait(), timeout=900)
            except asyncio.TimeoutError:
                pass
            return {"ok": True, "stopped": True, "finished": self.finished, "done": self.done.is_set()}
        if op == "step":
            if self.error:
                return {"ok": False, "error": self.error}
            if self.loop_done or self.aborted:
                return {"ok": False, "error": f"the stream {'aborted: ' + self.aborted if self.aborted else 'finished'}",
                        "aborted": self.aborted, "finished": self.finished}
            if self.running is not None:
                return {"ok": False, "busy": self.running, "next_index": self.next_index}
            if int(request.get("index", 0)) != self.next_index:
                return {"ok": False, "out_of_order": True, "next_index": self.next_index}
            future = asyncio.get_running_loop().create_future()
            await self.commands.put((request, future))
            return await future
        return {"ok": False, "error": f"unknown op {op!r}"}


async def serve(args) -> int:
    agent = json.loads(args.agent_json)
    environment = json.loads(args.environment_json)
    verifier = json.loads(args.verifier_json) if args.verifier_json else {}
    stream_dir = Path(args.stream_dir)
    config = TrialConfig(task=TaskConfig(path=Path(args.task_dir)), trial_name=args.name,
                         trials_dir=stream_dir / "trials", agent=AgentConfig(**agent),
                         environment=EnvironmentConfig(**environment), verifier=VerifierConfig(**verifier),
                         job_id=uuid4())
    trial = await StepDriverTrial.create(config)
    last_request = time.time()
    pending: list[asyncio.Future] = []

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal last_request
        last_request = time.time()
        try:
            line = await reader.readline()
            request = json.loads(line.decode() or "{}")
            reply = await trial.handle(request)
        except Exception as exc:                            # noqa: BLE001 - the client gets the error
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-2000:]}
        try:
            writer.write((json.dumps(reply, default=str) + "\n").encode())
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
        last_request = time.time()

    sock = Path(args.socket)
    sock.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(on_connect, path=str(sock))
    (stream_dir / "driver.json").write_text(json.dumps({"pid": os.getpid(), "socket": str(sock), "name": args.name,
                                                        "trial_dir": str(trial.paths.trial_dir),
                                                        "started": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
    log(f"listening on {sock}; trial {trial.paths.trial_dir}")

    async def request_stop(reason: str) -> None:
        if trial.loop_done:
            return
        log(f"stopping: {reason}")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        pending.append(future)
        await trial.commands.put(({"op": "stop"}, future))

    async def idle_watch() -> None:
        while not trial.loop_done:
            await asyncio.sleep(30)
            if trial.running is None and time.time() - last_request > args.idle_s:
                await request_stop(f"idle for {args.idle_s:.0f} s")
                return

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.ensure_future(request_stop(f"signal {s}")))
    watcher = asyncio.create_task(idle_watch())
    result = await trial.run()                               # _prepare, then _run (the request loop), then _finalize
    watcher.cancel()
    # A request queued after the loop ended (a step after an environment failure) is answered now.
    while not trial.commands.empty():
        _, future = trial.commands.get_nowait()
        if not future.done():
            future.set_result({"ok": False, "error": trial.error or "the stream finished", "finished": True})
    trial.done.set()
    await asyncio.sleep(0.5)                                 # a `stop` waiting on `done` writes its answer
    server.close()
    await server.wait_closed()
    sock.unlink(missing_ok=True)
    log(f"done: {'error ' + trial.error if trial.error else 'reward ' + str((result.verifier_result or {}) and result.verifier_result.rewards)}")
    return 0 if trial.error is None else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--stream-dir", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--agent-json", required=True)
    parser.add_argument("--environment-json", default="{}")
    parser.add_argument("--verifier-json", default="{}")
    parser.add_argument("--idle-s", type=float, default=4 * 3600)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(serve(args))
    except Exception as exc:                                 # noqa: BLE001 - the log says why the driver died
        log(f"fatal: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
