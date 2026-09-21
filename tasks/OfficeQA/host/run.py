"""Explicit paid entry point; importing other modules never starts inference."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import getpass
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

import campaign as c


def smoke(key, name):
    sources = {n:c.sha(c.ROOT/n) for n in ('acting.py','broker.py','campaign.py','rate_control.py','client.py','run.py')}
    root = c.ROOT / 'preflight' / name
    root.mkdir(parents=True, exist_ok=False)
    corpus = root / 'corpus'; corpus.mkdir()
    (corpus / 'fixture.txt').write_text('Synthetic transport test. There are no benchmark questions here.\n')
    index = root / 'index.sqlite'
    c.data._tools.build_index(corpus, index, expected_documents=1)
    marker = 'TRANSPORT_' + secrets.token_hex(6)
    def one(arm):
        base = root / arm
        workspace = base / 'workspace'
        adapter = c.W0 if arm == 'adapted' else None
        c.prepare_workspace(workspace, adapter, corpus=corpus, index=index)
        (workspace / 'note.txt').write_text(marker + '\n')
        prompt = ('This is a synthetic interface test. Use the native shell tool to read note.txt. '
                  'Return only the exact string inside note.txt as your final response.')
        if adapter:
            prompt += ' Also make one real wm MCP call to inspect the available memory before your final response.'
        with c.broker.Broker(key, base / 'api', max_completion=65536) as relay:
            result = c.acting.run_actor(workspace, base / 'records', prompt, corpus, index,
                                       adapter=adapter, endpoint=relay.endpoint, timeout=1800)
        answer = (base / 'records/final.md').read_text().strip() if (base / 'records/final.md').exists() else ''
        native = result['completed_item_counts']
        transport = c.read(base / 'api/summary.json')
        passed = (result['status'] == 'completed' and answer == marker and
                  native.get('command_execution', 0) >= 1 and not transport['failed'] and
                  (not adapter or native.get('mcp_tool_call',0) >= 1))
        return {'arm':arm,'passed':passed,'result':result,'transport':transport,
                'exact_fixture_answer':answer == marker}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(one, ('baseline','adapted')))
    result = {'passed':all(r['passed'] for r in results),'results':results,
              'source_hashes':sources,
              'model':c.MODEL,'benchmark_questions':0,'purpose':'Actual native shell and MCP round-trip through Fireworks Responses'}
    result['sources_unchanged'] = all(c.sha(c.ROOT/n)==h for n,h in sources.items())
    result['passed'] = result['passed'] and result['sources_unchanged']
    c.save(root / 'verification.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('smoke','run'))
    parser.add_argument('--name',default='native-roundtrip-01')
    args=parser.parse_args()
    if args.mode=='smoke':
        key=os.environ.get('FIREWORKS_API_KEY') or getpass.getpass('Fireworks API key (hidden): ')
        result=smoke(key,args.name)
        raise SystemExit(0 if result['passed'] else 1)
    checks = c.ROOT / 'preflight/readiness.json'
    if not checks.is_file() or not c.read(checks).get('passed'):
        raise ValueError('Formal campaign requires recorded passing readiness checks')
    readiness=c.read(checks)
    if not readiness.get('source_hashes') or any(c.sha(c.ROOT / name) != digest
            for name,digest in readiness['source_hashes'].items()):
        raise ValueError('Experiment source changed after readiness checks')
    if not readiness.get('gold_feedback_use_resolved'):
        raise ValueError('Gold-feedback dataset-use applicability has not been recorded')
    key=os.environ.get('FIREWORKS_API_KEY') or getpass.getpass('Fireworks API key (hidden): ')
    c.save(c.ROOT / 'process.json', {'pid':os.getpid(), 'pgid':os.getpgrp(),
                                  'started_utc':c.utc(), 'root':str(c.ROOT)})
    # Keep this authorized long run awake; the guard exits with this process.
    subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(os.getpid())],
                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    c.run(key)


if __name__=='__main__': main()
