#!/usr/bin/env python3
"""Run a candidate-only evolution session using the MWM method and Claude Code."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

from harnessadapter.adapter.loader import load_manifest
from harnessadapter.evolution.workspace import Workspace, copy_tree, fingerprint
from harnessadapter.policy.execution import ExecutionPolicy, PolicySandbox, load_policy
from harnessadapter.evolution.backends import command_for, codex_turn_outcome
from harnessadapter.evolution.definition import EvolverDefinition

from harnessadapter.paths import PACKAGE_ROOT, resolve_architecture, workspace_root


def evolve(source: Path, evidence: Path | None = None, goal: str = "Improve the adapter using supplied LearningMaterial.", timeout=240, budget=None, prepare_only=False, block=1, tick=0, policy=None, definition=None, source_kind='adapter', validation_data: Path | None = None, output: Path | None = None, model: str | None = None, reasoning_effort: str | None = None, learning_material: Path | None = None):
    from harnessadapter.adapter.resources import resolve_learning_material
    evidence = resolve_learning_material(learning_material, evidence)
    if evidence is None:
        raise ValueError("LearningMaterial is required")
    definition = definition or EvolverDefinition('mwm')
    policy = policy or definition.policy
    if policy.backend == 'codex' and budget is not None:
        raise ValueError('Codex has no equivalent CLI dollar-budget flag; use the timeout limit, or Claude Code for --max-budget-usd')
    if definition.requires_validation and validation_data is None:
        raise ValueError('This Evolver requires --validation storage')
    source, evidence = source.resolve(), evidence.resolve()
    if validation_data is not None:
        validation_data = validation_data.resolve()
        if not validation_data.is_dir() or not (validation_data/'README.md').is_file():
            raise ValueError('Validation storage must be a directory with README.md')
        for other in (source, evidence):
            if validation_data == other or validation_data in other.parents or other in validation_data.parents:
                raise ValueError('Validation storage must be separate from adapter and LearningMaterial')
    manifest=load_manifest(source)
    guide=manifest.get('evolver_readme','EVOLVER.md')
    from harnessadapter.adapter.loader import inside
    inside(source,guide)
    if not evidence.is_dir() or evidence == source or source in evidence.parents or evidence in source.parents:
        raise ValueError('LearningMaterial must be a separate directory outside the adapter')
    if not (evidence/'README.md').is_file():
        raise ValueError('LearningMaterial must provide README.md describing its records and structure')
    # Sessions default to the ignored .demo/evolution; a caller running a campaign
    # elsewhere names its own output root so its records never enter this repo.
    output = (output.resolve() if output is not None else workspace_root() / 'evolution')
    output.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix='evolve-', dir=output))
    for name in ('adapter', 'evidence', 'scratch', 'control', 'host', 'method', 'environment', 'history'):
        (session / name).mkdir()
    copy_tree(source, session / 'adapter')
    copy_tree(evidence, session / 'evidence')
    copy_tree(source, session / 'history')
    if validation_data is not None:
        copy_tree(validation_data, session/'validation')
    baseline = {'source': fingerprint(source), 'evidence': fingerprint(session / 'evidence'),
                'framework': fingerprint(PACKAGE_ROOT, exclude=('evolvers', 'architectures')), 'candidate': fingerprint(session / 'adapter')}
    (session / 'control/baseline.json').write_text(json.dumps(baseline, indent=2)+'\n')
    maintenance = definition.prepare_session(session/'adapter', session, block=block, tick=tick)
    # Evidence is an intrinsic read-only area, independent of optional environment grants.
    policy_document = {'requested': policy.serialize(), 'candidate': str(session/'adapter'),
                       'learning_material': str(session/'evidence'), 'evidence': str(session/'evidence'), 'scratch': str(session/'scratch'),
                       'write_scope': 'candidate-and-scratch-only',
                       'network_scope': 'agent tool/command traffic; model API transport is separate',
                       'executor': 'codex sandbox (shared by both learner backends)'}
    (session/'control/policy.json').write_text(json.dumps(policy_document,indent=2)+'\n')
    sandbox = PolicySandbox(session, policy)
    enforcement = sandbox.preflight()
    (session/'control/preflight.json').write_text(json.dumps(enforcement,indent=2)+'\n')
    environment_text = (
        '# Execution environment and LearningMaterial\n\n'
        'The adapter candidate is readable/writable. LearningMaterial is always readable and read-only; its README defines its format. MCP area learning_material and legacy area evidence resolve to the same immutable evidence/ snapshot. '
        'Method materials and history are also read-only. Only adapter and isolated scratch accept writes.\n\n'
        'Additional environment read scope: ' + json.dumps(policy.serialize()['read_access']) + '\n'
        'Default command working directory: ' + (policy.working_directory or str(session/'adapter')) + '\n'
        'Agent tool/command network: ' + ('enabled' if policy.network else 'disabled') + '. Model API transport remains available.\n\n'
        'Use get_policy for absolute roots; read_path/list_path for authorized environment files; run_command for sandboxed exploration; '
        'fetch_url for HTTP(S) when enabled. No benchmark-specific environment or evaluator is implicitly provided. '
        'Environment directories stay read-only even when used as a command working directory. '
        'MWM executable admission still uses its stricter test_python sandbox; ordinary run_command success is not admission.\n')
    (session/'environment/README.md').write_text(environment_text)
    protected_areas = ('method', 'environment', 'history') + (('validation',) if validation_data is not None else ())
    for area in protected_areas:
        baseline[area] = fingerprint(session / area)
    (session / 'control/baseline.json').write_text(json.dumps(baseline, indent=2)+'\n')
    prompt = definition.build_prompt(session/'adapter', session/'evidence', goal, guide)
    if validation_data is not None:
        prompt += '\n\nOptional validation is bound read-only at validation/. Read validation/README.md. These records are visible to you, not hidden held-out evaluation. No evaluator is run automatically; report which checks you actually performed.'
    prompt += '\n\nFramework execution policy:\n' + environment_text + '\nRead and obey it before interacting with the environment. All edits and execution use the evolution MCP tools. Native host tools are not an alternate permission path.'
    (session/'control/prompt.md').write_text(prompt)
    mcp = {'mcpServers': {'evolution': {'type':'stdio', 'command':str(Path(sys.executable).absolute()),
           'args':['-m', 'harnessadapter.evolution.server', '--session', str(session)]}}}
    config = session/'control/mcp.json'; config.write_text(json.dumps(mcp, indent=2)+'\n')
    result = {'session':str(session), 'source':str(source), 'candidate':str(session/'adapter'),
              'source_kind':source_kind,'candidate_kind':'adapter','learning_material':str(session/'evidence'),
              'validation_data':str(session/'validation') if validation_data is not None else None,
              'source_revision':hashlib.sha256(json.dumps(baseline['source'],sort_keys=True).encode()).hexdigest(),
              'evolver':str(definition.root), 'requested_model':model, 'reasoning_effort':reasoning_effort, 'meta_harness':policy.backend, 'activated':False, 'maintenance':maintenance,
              'execution_policy':policy_document, 'preflight':enforcement}
    if prepare_only:
        result['status']='prepared'
        (session/'control/result.json').write_text(json.dumps(result,indent=2)+'\n')
        return result
    command = command_for(session, policy, config, 3.0 if budget is None else budget, model=model, reasoning_effort=reasoning_effort)
    (session/'control/invocation.json').write_text(json.dumps({'argv':command,'cwd':str(session/'host'),'timeout':timeout},indent=2)+'\n')
    env=dict(os.environ);env['ENABLE_TOOL_SEARCH']='false'
    print(f'Evolution session: {session}',flush=True)
    with (session/'control/events.jsonl').open('w') as out, (session/'control/stderr.log').open('w') as err:
        proc=subprocess.Popen(command,cwd=session/'host',stdin=subprocess.PIPE,stdout=out,stderr=err,
                              text=True,env=env,start_new_session=True)
        try:
            proc.communicate(prompt,timeout=timeout)
            timed_out=False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid,signal.SIGTERM)
            try:proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid,signal.SIGKILL);proc.wait()
            timed_out=True
    events=[json.loads(s) for s in (session/'control/events.jsonl').read_text().splitlines() if s.strip()]
    if policy.backend == 'claude-code':
        final=next((e for e in reversed(events) if e.get('type')=='result'),{})
        (session/'control/report.md').write_text(final.get('result','')+'\n')
    else:
        final=codex_turn_outcome(events)
    admission = definition.finalize(session/'adapter',session)
    candidate=fingerprint(session/'adapter')
    unchanged={'source':fingerprint(source)==baseline['source'],
               'evidence':fingerprint(session/'evidence')==baseline['evidence'],
               'framework':fingerprint(PACKAGE_ROOT, exclude=('evolvers', 'architectures'))==baseline['framework']}
    for area in protected_areas:
        unchanged[area] = fingerprint(session / area) == baseline[area]
    validation=Workspace(session).validate()
    audit=session/'control/file-calls.jsonl'
    broker_calls=[json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
    broker_used=any(call.get('status')=='ok' for call in broker_calls)
    success=proc.returncode==0 and not timed_out and final.get('subtype')=='success' and not final.get('is_error') and broker_used
    result.update(status='candidate_ready' if success and all(unchanged.values()) and validation['passed'] else 'needs_review',
                  exit_code=proc.returncode,timed_out=timed_out,validation=validation,unchanged=unchanged,
                  changes=[p for p in sorted(candidate.keys() | baseline['candidate'].keys()) if candidate.get(p)!=baseline['candidate'].get(p)],
                  revision=hashlib.sha256(json.dumps(candidate,sort_keys=True).encode()).hexdigest(),
                  broker_calls_succeeded=sum(call.get('status')=='ok' for call in broker_calls),
                  admission=admission, total_cost_usd=final.get('total_cost_usd'),model=next((e.get('model') for e in events if e.get('subtype')=='init'),None))
    (session/'control/result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference',nargs='?',help='Adapter name (mwm/minimal-shop) or local adapter repo')
    parser.add_argument('--architecture', '--template', dest='architecture', help='Start from an architecture (prompt-only/mwm or local path); --template is a compatibility alias')
    parser.add_argument('--adapter',type=Path,help='Legacy form: positional reference is an Evolver; this selects the adapter')
    parser.add_argument('--evolver',help='Optional reusable Evolver preset or definition directory')
    prompts=parser.add_mutually_exclusive_group()
    prompts.add_argument('--prompt',help='Your update instructions; no Evolver files required')
    prompts.add_argument('--prompt-file',type=Path,help='Read your update instructions from Markdown')
    material = parser.add_mutually_exclusive_group(required=True)
    material.add_argument("--learning-material", dest="evidence", type=Path, help="Read-only LearningMaterial directory with README.md")
    material.add_argument("--evidence", dest="evidence", type=Path, help="Compatibility alias for --learning-material")
    parser.add_argument('--validation',type=Path,help='Optional read-only validation storage with README.md; no automatic evaluator')
    parser.add_argument('--goal',default='Improve the selected adapter using supplied LearningMaterial. Follow its evolution guide and report verified changes.')
    parser.add_argument('--timeout',type=int,default=240)
    parser.add_argument('--output',type=Path,help='Directory for evolution sessions (default: .demo/evolution inside this repo)')
    parser.add_argument('--model',help='Learner model id, overriding the client configuration (codex: -c model=...; Claude Code: --model)')
    parser.add_argument('--reasoning-effort',choices=['low','medium','high','xhigh'],help='Codex reasoning effort override')
    parser.add_argument('--max-budget-usd',type=float,default=None,help='Claude Code only; default 3 USD there')
    parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--block',type=int,default=1,help='Caller-supplied session number')
    parser.add_argument('--tick',type=int,default=0,help='Caller-supplied current tick; zero means unknown')
    parser.add_argument('--config',type=Path,help='Framework execution policy: TOML, JSON, or YAML')
    parser.add_argument('--backend',choices=['codex','claude-code'])
    parser.add_argument('--workdir',type=Path,help='Read-only environment working directory; commands default here, writes still only target candidate/scratch')
    reads=parser.add_mutually_exclusive_group()
    reads.add_argument('--read-dir',type=Path,action='append',default=None,help='Additional environment directory, read-only; repeatable')
    reads.add_argument('--read-all',action='store_true',help='Allow all filesystem reads; never expands write scope')
    net=parser.add_mutually_exclusive_group()
    net.add_argument('--network',dest='network',action='store_true',default=None)
    net.add_argument('--no-network',dest='network',action='store_false')
    args=parser.parse_args(argv)
    if args.architecture and (args.reference or args.adapter):parser.error('Choose an architecture or an adapter, not both')
    if not args.architecture and not args.reference:parser.error('Provide an adapter or --architecture')
    prompt_override=args.prompt_file.read_text() if args.prompt_file else args.prompt
    if args.adapter:
        source=args.adapter
        preset=args.evolver or args.reference
    else:
        reference=args.architecture or args.reference
        source=resolve_architecture(reference)
        # A user-supplied prompt selects the generic client, not a hidden MWM algorithm.
        preset=args.evolver or ('basic' if prompt_override is not None else 'mwm' if reference=='mwm' else 'basic')
    definition=EvolverDefinition(preset,prompt_override=prompt_override)
    policy=load_policy(args.config,backend=args.backend,read_paths=args.read_dir,read_all=args.read_all,network=args.network,defaults=definition.policy.serialize(),working_directory=args.workdir)
    kind='architecture' if args.architecture or (args.reference in ('mwm','prompt-only') and not args.adapter) else 'adapter'
    result=evolve(source,args.evidence,args.goal,args.timeout,args.max_budget_usd,args.prepare_only,args.block,args.tick,policy,definition,kind,validation_data=args.validation,output=args.output,model=args.model,reasoning_effort=args.reasoning_effort)
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result['status'] in ('prepared','candidate_ready') else 1)


if __name__=='__main__':
    main()
