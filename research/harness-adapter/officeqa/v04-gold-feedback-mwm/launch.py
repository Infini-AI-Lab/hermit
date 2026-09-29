"""Authorized live preflight then formal run; credential is held in memory only."""
import contextlib
import getpass
import io
import json
import os
from pathlib import Path
import subprocess

import campaign as c
import report
import run


def main():
    readiness = c.read(c.ROOT / 'preflight/readiness.json')
    if not readiness.get('offline_checks_passed'):
        raise ValueError('Offline checks have not passed')
    if any(c.sha(c.ROOT / name) != value for name,value in readiness['source_hashes'].items()):
        raise ValueError('Sources changed after recorded offline checks')
    if not readiness.get('gold_feedback_use_resolved'):
        raise ValueError('User protocol clarification has not been recorded')
    if any((c.ROOT / name).exists() for name in ('freeze.json','process.json','state.json')):
        raise ValueError('This fresh campaign has already started')
    key = getpass.getpass('Fireworks API key (hidden, memory only): ')
    if not key or key.strip() != key:
        raise ValueError('Credential is empty or has unexpected whitespace')
    subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(os.getpid())],
                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    print(json.dumps({'phase':'native_model_preflight','benchmark_questions':0}),flush=True)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            checked = run.smoke(key,'native-roundtrip-01')
        print(json.dumps({'phase':'native_model_preflight','passed':checked['passed'],
                          'arms':[{'arm':r['arm'],'passed':r['passed'],
                                   'status':r['result']['status'],
                                   'failure_kind':r['transport'].get('failure_kind')}
                                  for r in checked['results']]}),flush=True)
        readiness['paid_model_roundtrip_passed'] = checked['passed']
        readiness['paid_model_preflight'] = 'native-roundtrip-01/verification.json'
        readiness['credential_provided_to_controller_memory'] = True
        readiness['passed'] = checked['passed'] and checked['sources_unchanged']
        readiness['pending'] = [] if readiness['passed'] else ['Live native model preflight failed']
        c.save(c.ROOT / 'preflight/readiness.json',readiness)
        if not readiness['passed']:
            c.save(c.ROOT / 'setup-status.json',{'status':'live_preflight_failed',
                   'paid_inference_started':False,'pending':readiness['pending'],
                   'formal_benchmark_started':False})
            report.build(c.ROOT)
            return 1
        c.save(c.ROOT / 'process.json',{'pid':os.getpid(),'pgid':os.getpgrp(),
               'started_utc':c.utc(),'root':str(c.ROOT),'launcher_sha256':c.sha(__file__)})
        c.save(c.ROOT / 'setup-status.json',{'status':'running',
               'paid_inference_started':True,'formal_benchmark_started':True,'pending':[]})
        c.run(key)
        return 0
    except Exception as exc:
        print(json.dumps({'error':(type(exc).__name__+': '+str(exc)).replace(key,'[REDACTED]')}),flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
