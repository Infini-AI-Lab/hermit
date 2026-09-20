#!/usr/bin/env python3
"""Launch the same deploy/evolve CLI workflow from safe YAML configuration."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import yaml
from harnessadapter.paths import BUILTIN_EVOLVERS, BUILTIN_ARCHITECTURES



def compile_launch(path: Path, prepare_only=False):
    path=path.resolve();data=yaml.safe_load(path.read_text())
    if not isinstance(data,dict):raise ValueError('Launch YAML must contain a mapping')
    allowed={'action','adapter','adapters','architecture','template','learning_material','evidence','validation','prompt','prompt_file','evolver',
             'backend','model','reasoning_effort','host','workdir','read_access','network','prepare_only','timeout','max_budget_usd'}
    if set(data)-allowed:raise ValueError('Unknown launch fields: '+', '.join(sorted(set(data)-allowed)))
    if 'learning_material' in data and 'evidence' in data:
        raise ValueError('Specify learning_material or legacy evidence, not both')
    if 'learning_material' in data:
        data['evidence'] = data['learning_material']
    action=data.get('action')
    if action not in ('deploy','evolve'):raise ValueError('action must be deploy or evolve')
    def string(key):
        value=data.get(key)
        if value is not None and (not isinstance(value,str) or not value.strip()):raise ValueError(key+' must be a nonempty string')
        return value
    def location(value,reference=False):
        if reference and value in (set(BUILTIN_ARCHITECTURES) | {'minimal-shop'} | BUILTIN_EVOLVERS):return value
        p=Path(value).expanduser();return str((path.parent/p).resolve() if not p.is_absolute() else p.resolve())
    adapters=data.get('adapters')
    adapter=string('adapter')
    if string('architecture') and string('template'):raise ValueError('Use architecture, not both architecture and template')
    template=string('architecture') or string('template')
    if adapters is not None:
        if adapter or template or not isinstance(adapters,list) or not all(isinstance(x,str) for x in adapters):
            raise ValueError('Use adapter/architecture or adapters, not both')
        if len(adapters)!=1:raise ValueError('Multi-adapter deployment is not implemented yet; no configuration was applied')
        adapter=adapters[0]
    if bool(adapter)==bool(template):raise ValueError('Specify exactly one adapter or architecture')
    cmd=[action]
    if action=='deploy':
        if template:raise ValueError('Evolve an architecture into an adapter before deploying through launch YAML')
        if any(k in data for k in ('validation','prompt','prompt_file','evolver','backend','model','reasoning_effort','read_access','network','timeout','max_budget_usd')):
            raise ValueError('Evolution-only fields are not valid for deploy')
        cmd.append(location(adapter,True))
        host=string('host') or 'codex'
        if host not in ('codex','claude-code'):raise ValueError('Unsupported acting host')
        cmd.extend(['--host',host])
    else:
        if 'host' in data or adapters is not None:raise ValueError('Evolution uses backend and a single adapter/architecture')
        cmd.extend(['--architecture',location(template,True)] if template else [location(adapter,True)])
        if string('validation'):cmd.extend(['--validation',location(data['validation'])])
        if not string('evidence'):raise ValueError('Evolution requires LearningMaterial')
        if string('prompt') and string('prompt_file'):raise ValueError('Choose prompt or prompt_file')
        if string('prompt'):cmd.extend(['--prompt',data['prompt']])
        if string('prompt_file'):cmd.extend(['--prompt-file',location(data['prompt_file'])])
        if string('evolver'):cmd.extend(['--evolver',location(data['evolver'],True)])
        if string('model'):cmd.extend(['--model',data['model']])
        if string('reasoning_effort'):
            if data['reasoning_effort'] not in ('low','medium','high','xhigh'):raise ValueError('Unsupported reasoning effort')
            cmd.extend(['--reasoning-effort',data['reasoning_effort']])
        backend=string('backend')
        if backend:
            if backend not in ('codex','claude-code'):raise ValueError('Unsupported learner backend')
            cmd.extend(['--backend',backend])
        if 'network' in data:
            if not isinstance(data['network'],bool):raise ValueError('network must be true or false')
            cmd.append('--network' if data['network'] else '--no-network')
        if 'read_access' in data:
            access=data['read_access']
            if access=='all':cmd.append('--read-all')
            elif isinstance(access,dict) and set(access)=={'paths'} and isinstance(access['paths'],list):
                for value in access['paths']:
                    if not isinstance(value,str) or not value.strip():raise ValueError('Invalid read path')
                    cmd.extend(['--read-dir',location(value)])
            else:raise ValueError('read_access must be all or a paths list')
        for key,flag in [('timeout','--timeout'),('max_budget_usd','--max-budget-usd')]:
            if key in data:
                value=data[key]
                if isinstance(value,bool) or not isinstance(value,(int,float)) or value<=0:raise ValueError(key+' must be positive')
                cmd.extend([flag,str(value)])
    if string('workdir'):cmd.extend(['--workdir',location(data['workdir'])])
    if string('evidence'):cmd.extend(['--learning-material',location(data['evidence'])])
    if 'prepare_only' in data and not isinstance(data['prepare_only'],bool):raise ValueError('prepare_only must be boolean')
    if prepare_only or data.get('prepare_only',False):cmd.append('--prepare-only')
    return cmd


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config',type=Path);parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args(argv)
    try:
        cmd=compile_launch(args.config,args.prepare_only)
        if args.dry_run:print(json.dumps({'argv':['harnessadapter',*cmd]},indent=2))
        else:raise SystemExit(subprocess.call([sys.executable,'-m','harnessadapter',*cmd]))
    except (ValueError,OSError,yaml.YAMLError) as exc:
        parser.error(str(exc))


if __name__=='__main__':
    main()
