#!/usr/bin/env python3
"""One filesystem operation inside the OS sandbox, with the framework policy."""
import argparse
import json
from pathlib import Path
from execution_policy import ExecutionPolicy, PolicySandbox
from evolution_workspace import Workspace


def execute(session, name, args):
    document=json.loads((session/'control/policy.json').read_text())
    policy=ExecutionPolicy.parse(document['requested'])
    sandbox=PolicySandbox(session,policy)
    if name in ('read_path','list_path'):
        path=Path(args['path']).expanduser().resolve()
        if not sandbox.can_read(path):raise ValueError('Path is outside the user-defined readable scope')
        if name=='read_path':
            offset=args.get('offset',0)
            if offset<0:raise ValueError('offset must be nonnegative')
            with path.open() as stream:
                remaining=offset
                while remaining:
                    chunk=stream.read(min(remaining,8192))
                    if not chunk:break
                    remaining-=len(chunk)
                text=stream.read(30001)
            return {'path':str(path),'text':text[:30000],'next_offset':offset+30000 if len(text)>30000 else None}
        entries=sorted(p.name for p in path.iterdir());offset=args.get('offset',0)
        if offset<0:raise ValueError('offset must be nonnegative')
        return {'path':str(path),'entries':entries[offset:offset+200],
                'next_offset':offset+200 if len(entries)>offset+200 else None}
    if name not in ('list_files','read_file','write_file','delete_path','move_path','validate'):
        raise ValueError('Unknown filesystem capability')
    return getattr(Workspace(session),name)(**args)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session',type=Path,required=True);parser.add_argument('--name',required=True)
    parser.add_argument('--arguments',required=True);args=parser.parse_args()
    try:reply={'result':execute(args.session.resolve(),args.name,json.loads(args.arguments))}
    except Exception as exc:reply={'error':f'{type(exc).__name__}: {exc}'}
    print(json.dumps(reply))
