"""Derive a v9 candidate profile/env from the recorded v8 one (scratch only, nothing installed)."""
import json, os, sys
S = sys.argv[1]
BASEPY = '/Users/albertzhao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python'
VENV = '/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv'
HA = '/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/frameworks-v8/next-a48c0f9/harnessadapter'
NODE = '/Applications/ChatGPT.app/Contents/Resources/cua_node'
RG = '/Applications/ChatGPT.app/Contents/Resources/rg'
TOOLS = S + '/tools'
prof = open(S + '/v8.sb').read()
# narrower: actor no longer needs the controller venv, the framework package or node
for root in (VENV, HA, NODE):
    prof = prof.replace(' (subpath "%s")' % root, '')
# tools dir (shims + zprofile) and the single bundled rg binary become readable/executable
prof = prof.replace('(subpath "/System")', '(subpath "/System") (subpath "%s") (literal "%s")' % (TOOLS, RG), 1)
open(S + '/v9.sb', 'w').write(prof)
os.makedirs(TOOLS + '/bin', exist_ok=True)
for name, target in (('python3', BASEPY + '/bin/python3'), ('python', BASEPY + '/bin/python3'), ('rg', RG)):
    link = TOOLS + '/bin/' + name
    if not os.path.lexists(link):
        os.symlink(target, link)
env = json.load(open(S + '/env.json'))
env['PATH'] = TOOLS + '/bin:/usr/bin:/bin:/usr/sbin:/sbin'
env['TMPPREFIX'] = env['TMPDIR'] + '/zsh'
env['ZDOTDIR'] = TOOLS + '/zdot'
os.makedirs(TOOLS + '/zdot', exist_ok=True)
open(TOOLS + '/zdot/.zprofile', 'w').write('# undo /etc/zprofile path_helper reordering\nexport PATH="%s"\n' % env['PATH'])
open(S + '/env9.args', 'w').write('\n'.join(f'{k}={v}' for k, v in env.items()) + '\n')
print(json.dumps(env, indent=1))
