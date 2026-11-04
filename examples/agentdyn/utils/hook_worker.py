"""Container-only JSON hook worker. No benchmark or judge code is mounted here."""
import asyncio
from dataclasses import asdict
import importlib.util
import inspect
import json
import sys
import types

wire = sys.stdout
sys.stdout = sys.stderr  # Candidate prints cannot corrupt protocol framing.

def send(value):
    wire.write(json.dumps(value) + '\n')
    wire.flush()

# Install only the portable SDK under its public import path.
name = 'hermit.agent.evolver.methods.evosafe.hooks'
for n in range(1, len(name.split('.'))):
    parent = '.'.join(name.split('.')[:n])
    module = types.ModuleType(parent)
    module.__path__ = []
    sys.modules[parent] = module
spec = importlib.util.spec_from_file_location(name, '/sdk.py')
sdk = importlib.util.module_from_spec(spec)
sys.modules[name] = sdk
spec.loader.exec_module(sdk)
# Compatibility import for existing bundles.
sys.modules['hermit.integrations'] = types.ModuleType('hermit.integrations')
sys.modules['hermit.integrations.evosafe_hooks'] = sdk

async def invoke(fn, *args):
    result = fn(*args)
    return await result if inspect.isawaitable(result) else result

def classify(text):
    if not isinstance(text, str):
        raise TypeError('classifier input must be text')
    send({'classify': text})
    return json.loads(sys.stdin.readline())['classification']

def context(value, enabled):
    ctx = sdk.ToolCallContext(**value)
    ctx.classify_output = classify if enabled else None
    return ctx

candidate = types.ModuleType('_candidate')
candidate.__file__ = '/defense.py'
sys.modules['_candidate'] = candidate
namespace = candidate.__dict__
exec(compile(open('/defense.py').read(), '/defense.py', 'exec'), namespace)
defense = namespace['build']()
for method in ('system_prompt_transform', 'on_pre_tool_call', 'on_post_tool_call'):
    if not callable(getattr(defense, method, None)):
        raise ValueError('missing hook method')
api = getattr(defense, 'hook_api', 'dict-v1')
if api not in ('dict-v1', 'structured-v1'):
    raise ValueError('unsupported hook API')
send({'ready': api})
for line in sys.stdin:
    try:
        request = json.loads(line)
        method = request['method']
        args = request['args']
        ctx = None
        result = None
        if method != 'system_prompt_transform' and api == 'structured-v1':
            ctx = context(args[0], request.get('classifier', False))
            args[0] = ctx
            if method == 'on_post_tool_call':
                result = sdk.ToolCallResult(**args[1])
                args[1] = result
        value = asyncio.run(invoke(getattr(defense, method), *args))
        if ctx is not None and method == 'on_pre_tool_call':
            if isinstance(value, dict):
                # A decision dict keeps the custom metadata the hook set for its post-hook.
                send({'value': value, 'metadata': ctx.metadata})
            else:
                ctx = value if value is not None else ctx
                ctx.classify_output = None
                send({'context': asdict(ctx)})
        elif result is not None:
            result = value if value is not None else result
            if isinstance(result.exception, PermissionError):
                send({'refused': str(result.exception)})
            elif result.exception is not None:
                send({'error': 'hook returned an exception'})
            else:
                send({'result': asdict(result)})
        else:
            send({'value': value})
    except PermissionError as exc:
        send({'refused': str(exc)})
    except Exception as exc:
        send({'error': type(exc).__name__})
