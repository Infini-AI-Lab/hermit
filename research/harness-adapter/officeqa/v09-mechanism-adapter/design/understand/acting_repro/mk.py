"""Rebuild the recorded v8 Seatbelt profile + actor env with the workspace moved to scratch."""
import json, sys
P = '/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/officeqa-pro133-none-v8-work/runs/baseline/001'
S = sys.argv[1]
inv = json.load(open(P + '/records/invocation.json'))
prof = inv['argv'][2].replace(P + '/workspace', S + '/ws')
open(S + '/v8.sb', 'w').write(prof)
env = inv['settings']['shell_environment_policy.set']
env = {k: v.replace(P + '/workspace', S + '/ws') for k, v in env.items()}
json.dump(env, open(S + '/env.json', 'w'), indent=1)
open(S + '/env.args', 'w').write('\n'.join(f'{k}={v}' for k, v in env.items()) + '\n')
print(env['PATH'])
