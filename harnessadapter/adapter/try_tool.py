"""Run one declared tool of a candidate adapter in a child process under the
session's OS sandbox, with NO network: every resource binding the tool uses
(search, documents, small_model, sql) is a proxy whose calls travel as JSON
lines over this process's stdout/stdin to the evolution broker, which holds
the real bindings and does the HTTP. Candidate code therefore never runs in
the broker's process and never opens a socket.

    python -m harnessadapter.adapter.try_tool --adapter DIR --tool NAME \
        --arguments JSON --binding NAME[:DESCRIPTION] ...

Protocol on stdout (one JSON object per line):
    {"rpc": n, "binding": name, "method": m, "args": [...], "kwargs": {...}}
      -> the broker answers on stdin {"rpc": n, "result": ...} | {"rpc": n, "error": "..."}
    {"done": {"result": ...} | {"error": "...", "traceback": "..."}}
"""
import argparse
import json
import sys
import traceback


class _Channel:
    def __init__(self, out, inp):
        self.out, self.inp, self.n = out, inp, 0

    def call(self, binding, method, args, kwargs):
        self.n += 1
        self.out.write(json.dumps({'rpc': self.n, 'binding': binding, 'method': method,
                                   'args': list(args), 'kwargs': dict(kwargs)}, default=str) + '\n')
        self.out.flush()
        line = self.inp.readline()
        if not line:
            raise RuntimeError('the broker closed the binding channel')
        reply = json.loads(line)
        if reply.get('rpc') != self.n:
            raise RuntimeError('binding channel out of sequence')
        if 'error' in reply:
            raise RuntimeError(reply['error'])
        return reply.get('result')


class ProxyBinding:
    """Stands in for one bound resource: any method call is forwarded."""

    def __init__(self, channel, name, description):
        self._channel, self._name, self._description = channel, name, description

    def describe(self):
        return self._description

    def __getattr__(self, method):
        if method.startswith('_'):
            raise AttributeError(method)
        return lambda *args, **kwargs: self._channel.call(self._name, method, args, kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--adapter', required=True)
    parser.add_argument('--tool', required=True)
    parser.add_argument('--arguments', default='{}')
    parser.add_argument('--binding', action='append', default=[], help='NAME[:DESCRIPTION]')
    args = parser.parse_args(argv)
    protocol_out, sys.stdout = sys.stdout, sys.stderr      # tool prints go to stderr, the channel keeps stdout
    channel = _Channel(protocol_out, sys.stdin)
    try:
        resources = {}
        for item in args.binding:
            name, _, description = item.partition(':')
            resources[name] = ProxyBinding(channel, name, description)
        from harnessadapter.adapter.bindings import call_tool_with_resources
        result = call_tool_with_resources(args.adapter, resources, args.tool, json.loads(args.arguments))
        protocol_out.write(json.dumps({'done': {'result': result}}, default=str) + '\n')
    except Exception as exc:  # noqa: BLE001 — the caller sees the tool's failure as data
        protocol_out.write(json.dumps({'done': {'error': f'{type(exc).__name__}: {exc}',
                                                'traceback': traceback.format_exc()[-2000:]}}) + '\n')
    protocol_out.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
