"""The process that runs the acting agent's program, inside the sandbox `dojo.Engine` made for it (no network; the
program's directory and the Python environment, nothing of RoboDojo's checkout). It speaks with the policy server
(hermit_program/model.py) over its own stdin and stdout; the program's prints go to stderr.

    python worker.py <program.py>

The program defines `act(obs) -> list[dict]` (an action chunk) and may define `reset()`, called before each
episode. The module is loaded once, so its globals last across episodes; `reset` is where an episode's state goes.
"""
import json
import os
import pickle
import struct
import sys
import traceback


def main() -> None:
    channel_in = os.fdopen(os.dup(sys.stdin.fileno()), "rb")
    channel_out = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    sys.stdin = open(os.devnull)
    sys.stdout = sys.stderr                     # the program's prints must not reach the channel
    namespace = {"__name__": "policy"}
    loaded = None
    try:
        with open(sys.argv[1]) as handle:
            exec(compile(handle.read(), sys.argv[1], "exec"), namespace)
        if not callable(namespace.get("act")):
            loaded = "the program defines no act(obs) function"
    except BaseException:                       # noqa: BLE001 - reported on the first call
        loaded = "the program could not be loaded:\n" + traceback.format_exc(limit=8)

    def plain(value):
        """An action chunk as JSON: numpy arrays and scalars as lists and numbers."""
        if hasattr(value, "tolist"):
            return value.tolist()
        if isinstance(value, dict):
            return {str(k): plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(v) for v in value]
        return value

    def unpacked(value):
        """The observation's arrays back as numpy arrays (model.py: `packed`), read-only."""
        if isinstance(value, dict):
            if set(value) == {"__nd__"}:
                import numpy
                dtype, shape, data = value["__nd__"]
                return numpy.frombuffer(data, dtype=numpy.dtype(dtype)).reshape(shape)
            return {k: unpacked(v) for k, v in value.items()}
        if isinstance(value, list):
            return [unpacked(v) for v in value]
        return value

    while True:
        head = channel_in.read(8)
        if len(head) < 8:
            return
        message = pickle.loads(channel_in.read(struct.unpack("<Q", head)[0]))   # from the policy server: trusted
        try:
            if loaded:
                raise RuntimeError(loaded)
            if message["op"] == "reset":
                if callable(namespace.get("reset")):
                    namespace["reset"]()
                answer = {"ok": True}
            else:
                answer = {"ok": True, "actions": plain(namespace["act"](unpacked(message["obs"])))}
            data = json.dumps(answer, allow_nan=False).encode()
        except BaseException:                   # noqa: BLE001 - the program's error is its answer
            data = json.dumps({"ok": False, "error": traceback.format_exc(limit=8)[-4000:]}).encode()
        channel_out.write(struct.pack("<Q", len(data)) + data)
        channel_out.flush()


if __name__ == "__main__":
    main()
