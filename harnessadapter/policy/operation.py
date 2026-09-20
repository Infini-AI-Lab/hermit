#!/usr/bin/env python3
"""One filesystem operation inside the OS sandbox, with the framework policy."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
from harnessadapter.policy.execution import ExecutionPolicy, PolicySandbox
from harnessadapter.evolution.workspace import Workspace


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
    if name == 'snapshot_preview':
        if args['area'] not in ('adapter', 'scratch'):
            raise ValueError('Preview source must be in adapter or scratch')
        path = Workspace(session).path(args['area'], args['path'])
        if path.suffix.lower() not in ('.html', '.htm'):
            raise ValueError('Preview requires a self-contained HTML file')
        data = path.read_bytes()
        if len(data) > 1_000_000:
            raise ValueError('Preview HTML exceeds 1 MB')
        return {'html': data.decode('utf-8'), 'sha256': hashlib.sha256(data).hexdigest()}
    if name == 'read_image':
        path=Workspace(session).path(args['area'],args['path'])
        if path.suffix.lower() not in ('.png','.jpg','.jpeg'):
            raise ValueError('read_image accepts PNG or JPEG files only')
        with path.open('rb') as stream:
            data=stream.read(4*1024*1024+1)
        if len(data)>4*1024*1024:
            raise ValueError('Image exceeds the 4 MiB limit')
        if data.startswith(b'\x89PNG\r\n\x1a\n'): mime='image/png'
        elif data.startswith(b'\xff\xd8\xff'): mime='image/jpeg'
        else: raise ValueError('File does not have a PNG or JPEG signature')
        return {'area':args['area'],'path':args['path'],'mime_type':mime,
                'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),
                'data':base64.b64encode(data).decode('ascii')}
    if name not in ('list_files' ,'read_file','write_file','delete_path','move_path','validate'):
        raise ValueError('Unknown filesystem capability')
    return getattr(Workspace(session),name)(**args)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session',type=Path,required=True);parser.add_argument('--name',required=True)
    parser.add_argument('--arguments',required=True);args=parser.parse_args()
    try:reply={'result':execute(args.session.resolve(),args.name,json.loads(args.arguments))}
    except Exception as exc:reply={'error':f'{type(exc).__name__}: {exc}'}
    print(json.dumps(reply))
