#!/usr/bin/env python3
"""harnessadapter evolve: run one evolution session and return a candidate adapter.

A learner Evolver (basic, mwm, ...) is a prompt for a sandboxed Codex or
Claude Code session that edits the candidate from LearningMaterial. An
optimizer Evolver (gepa, skillopt, any evolver.toml with kind = "optimizer")
is a search plugin that scores candidates in an Environment; the same command
runs it (harnessadapter.evolution.optimizer)."""
import argparse
import hashlib
import importlib.util
import json
import shutil
import re
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

from harnessadapter.paths import PACKAGE_ROOT, resolve_architecture, workspace_root, BUILTIN_ARCHITECTURES


def learner_usage(events):
    """Token usage of the learner's own model calls, summed over the backend's
    turn events: codex reports one `turn.completed` with `usage` per turn,
    claude-code one `result` with `usage`. Zero when a backend reports none."""
    usage = {'input': 0, 'cached': 0, 'output': 0, 'reasoning': 0, 'turns': 0}
    for event in events:
        if event.get('type') == 'turn.completed':
            u = event.get('usage') or {}
            usage['input'] += int(u.get('input_tokens') or 0)
            usage['cached'] += int(u.get('cached_input_tokens') or 0)
            usage['output'] += int(u.get('output_tokens') or 0)
            usage['reasoning'] += int(u.get('reasoning_output_tokens') or 0)
            usage['turns'] += 1
        elif event.get('type') == 'result' and isinstance(event.get('usage'), dict):
            u = event['usage']
            usage['input'] += int(u.get('input_tokens') or 0)
            usage['cached'] += int(u.get('cache_read_input_tokens') or 0)
            usage['output'] += int(u.get('output_tokens') or 0)
            usage['turns'] += 1
    return usage


def evolve(source: Path, evidence: Path | None = None, goal: str = "Improve the adapter using supplied LearningMaterial.", timeout=240, budget=None, prepare_only=False, block=1, tick=0, policy=None, definition=None, source_kind='adapter', validation_data: Path | None = None, output: Path | None = None, model: str | None = None, reasoning_effort: str | None = None, learning_material: Path | None = None, preview_enabled: bool = False, bindings: list | None = None, deployment: Path | None = None, notes: str = '', provider: dict | None = None, mounts: dict | None = None):
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
    for name in ('adapter', 'evidence', 'scratch', 'control', 'host', 'method', 'environment', 'history', 'observations'):
        (session / name).mkdir()
    copy_tree(source, session / 'adapter')
    copy_tree(evidence, session / 'evidence')
    copy_tree(source, session / 'history')
    # Revision history rides with the candidate: the source's repository is
    # carried over (or started here) and the framework commits the outcome.
    from harnessadapter.adapter import versioning
    versioning.copy_history(source, session / 'adapter')
    source_head = versioning.ensure_repo(session / 'adapter')
    if deployment is not None:
        deployment = deployment.resolve()
        if not deployment.is_dir():
            raise ValueError('Deployment must be a directory')
        copy_tree(deployment, session / 'deployment')
    if validation_data is not None:
        copy_tree(validation_data, session/'validation')
    # Environment mounts: host paths the learner reads at /environment/<name>
    # (a directory or a file), read-only, in the sandbox and through the
    # broker alike — the environment's own data, never copied into the session.
    requested_mounts = dict(mounts or {})
    mounts = {}
    for name, target in requested_mounts.items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name) or name in ('README.md', 'FRAMEWORK.md'):
            raise ValueError(f'Mount name must be a plain file name: {name!r}')
        target = Path(target).resolve()
        if not target.exists():
            raise ValueError(f'Mount target does not exist: {target}')
        placeholder = session / 'environment' / name
        if target.is_dir():
            placeholder.mkdir(parents=True, exist_ok=True)
        else:
            placeholder.parent.mkdir(parents=True, exist_ok=True)
            placeholder.touch()
        mounts[name] = str(target)
    (session / 'control/mounts.json').write_text(json.dumps(mounts) + '\n')
    baseline = {'source': fingerprint(source), 'evidence': fingerprint(session / 'evidence'),
                'framework': fingerprint(PACKAGE_ROOT, exclude=('evolvers', 'architectures')), 'candidate': fingerprint(session / 'adapter')}
    (session / 'control/baseline.json').write_text(json.dumps(baseline, indent=2)+'\n')
    maintenance = definition.prepare_session(session/'adapter', session, block=block, tick=tick)
    # The definition's immutable core (its interface and library code): the
    # broker refuses writes under these paths and finalize refuses the
    # candidate if they changed by any other route (a shell in the sandbox).
    protected_paths = list(getattr(definition.method, 'PROTECTED_PATHS', ()) or definition.protected or ())
    (session / 'control/protected.json').write_text(json.dumps(protected_paths) + '\n')
    # The reference for the protected files is the adapter AFTER the
    # definition prepared it (prepare_session writes CONTRACT.md itself).
    baseline['protected'] = Workspace(session).protected_files(fingerprint(session / 'adapter'))
    # Evidence is an intrinsic read-only area, independent of optional environment grants.
    policy_document = {'requested': policy.serialize(), 'candidate': str(session/'adapter'),
                       'learning_material': str(session/'evidence'), 'evidence': str(session/'evidence'), 'scratch': str(session/'scratch'),
                       'write_scope': 'candidate-and-scratch-only',
                       'network_scope': 'agent tool/command traffic; model API transport is separate',
                       'preview_enabled': bool(preview_enabled),
                       'bindings': list(bindings or []),
                       'mounts': mounts,
                       'deployment': str(session/'deployment') if deployment is not None else None,
                       'notes': notes or '',
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
    if bindings:
        names = [str(b).split('=',1)[0] for b in bindings]
        environment_text += ('\nBound resources: ' + ', '.join(names) + '. Session tools reach them through the broker; commands inside the sandbox cannot. '
            + ('try_tool(name, arguments) loads the current candidate with the bindings and calls one declared tool. ' if any(n in ('search','documents','small_model') for n in names) else '')
            + ('small_model(messages, max_tokens) calls the bound reader model. ' if 'small_model' in names else '')
            + ('sql(query) runs one read-only statement against the environment database (SELECT/WITH/PRAGMA/EXPLAIN; rows and characters capped; every statement is logged). ' if 'sql' in names else '')
            + '\n')
    if deployment is not None:
        environment_text += '\nDeployment texts for this run are readable under area deployment (contract/, host.json); the method has already rendered them into its contract.\n'
    environment_text += '\nRevision history: the candidate adapter is a git repository maintained by the framework; history(n) lists commits, history(ref=..., path=...) shows a diff. You do not run git yourself.\n'
    environment_text += ('\nBrowser preview service: ' + ('enabled. Use request_preview for a single self-contained HTML file in adapter/scratch; then get_preview and read_image in observations. At most two requests. Rendering is a diagnostic, not independent quality feedback.' if preview_enabled else 'not bound. Do not wait for preview results.') + '\n')
    native_env=(session/'deployment/environment/README.md') if deployment is not None else None
    if native_env is not None and native_env.is_file():
        # The deployment supplies the environment surface the learner reads
        # (what a host can reach, nothing more); the framework's own policy
        # text is kept beside it for reference. Other files of the
        # deployment's environment directory (a client for a channel) ride
        # along at /environment/<name>.
        (session/'environment/README.md').write_text(native_env.read_text())
        (session/'environment/FRAMEWORK.md').write_text(environment_text)
        for extra in sorted(native_env.parent.iterdir()):
            if extra.is_file() and extra.name != 'README.md' and not (session/'environment'/extra.name).exists():
                shutil.copy2(extra, session/'environment'/extra.name)
    else:
        (session/'environment/README.md').write_text(environment_text)
    protected_areas = ('method', 'environment', 'history') + (('validation',) if validation_data is not None else ())
    for area in protected_areas:
        baseline[area] = fingerprint(session / area)
    (session / 'control/baseline.json').write_text(json.dumps(baseline, indent=2)+'\n')
    prompt = definition.build_prompt(session/'adapter', session/'evidence', goal, guide, notes=notes)
    if validation_data is not None:
        prompt += '\n\nOptional validation is bound read-only at validation/. Read validation/README.md. These records are visible to you, not hidden held-out evaluation. No evaluator is run automatically; report which checks you actually performed.'
    if not getattr(definition.method, 'NATIVE_PROMPT', False):
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
    command = command_for(session, policy, config, 3.0 if budget is None else budget, model=model, reasoning_effort=reasoning_effort, provider=provider)
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
    gate_findings = list((admission or {}).get('gates') or [])
    candidate=fingerprint(session/'adapter')
    committed = versioning.commit_all(session/'adapter', f'evolve: block {block} session {session.name}') if candidate != baseline['candidate'] else None
    unchanged={'source':fingerprint(source)==baseline['source'],
               'evidence':fingerprint(session/'evidence')==baseline['evidence'],
               'framework':fingerprint(PACKAGE_ROOT, exclude=('evolvers', 'architectures'))==baseline['framework']}
    for area in protected_areas:
        unchanged[area] = fingerprint(session / area) == baseline[area]
    workspace = Workspace(session)
    unchanged['protected'] = workspace.protected_files(candidate) == baseline['protected']
    validation=workspace.validate()
    usage = learner_usage(events)
    audit=session/'control/file-calls.jsonl'
    broker_calls=[json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
    broker_used=any(call.get('status')=='ok' for call in broker_calls)
    success=proc.returncode==0 and not timed_out and final.get('subtype')=='success' and not final.get('is_error') and broker_used
    # Gate findings are advice for the next session (native V2 behaviour:
    # gates.check() reports, it does not revert); they never block adoption.
    result.update(status='candidate_ready' if success and all(unchanged.values()) and validation['passed'] else 'needs_review',
                  gates=gate_findings, source_head=source_head, candidate_commit=committed,
                  exit_code=proc.returncode,timed_out=timed_out,validation=validation,unchanged=unchanged,
                  changes=[p for p in sorted(candidate.keys() | baseline['candidate'].keys()) if candidate.get(p)!=baseline['candidate'].get(p)],
                  revision=hashlib.sha256(json.dumps(candidate,sort_keys=True).encode()).hexdigest(),
                  broker_calls_succeeded=sum(call.get('status')=='ok' for call in broker_calls),
                  usage=usage,
                  admission=admission, total_cost_usd=final.get('total_cost_usd'),model=next((e.get('model') for e in events if e.get('subtype')=='init'),None))
    (session/'control/result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


# Options that belong to one kind of Evolver; the other kind refuses them.
LEARNER_OPTIONS=('evidence','prompt','prompt_file','validation','goal','timeout','max_budget_usd','prepare_only','block','tick',
                 'config','backend','workdir','deployment','notes','provider_name','provider_wire_api','bind','mount',
                 'read_dir','read_all','network')
OPTIMIZER_OPTIONS=('environment','environment_config','settings','set','component','training_state','max_tokens','temperature',
                   'scripted_model')


def main(argv=None):
    parser=argparse.ArgumentParser(prog='harnessadapter evolve',description=__doc__)
    parser.add_argument('reference',nargs='?',help='Adapter name (mwm/minimal-shop) or local adapter repo')
    parser.add_argument('--architecture', '--template', dest='architecture', help='Start from an architecture (prompt-only/mwm or local path); --template is a compatibility alias')
    parser.add_argument('--adapter',type=Path,help='Legacy form: positional reference is an Evolver; this selects the adapter')
    parser.add_argument('--evolver',help='Optional reusable Evolver preset or definition directory')
    prompts=parser.add_mutually_exclusive_group()
    prompts.add_argument('--prompt',help='Your update instructions; no Evolver files required')
    prompts.add_argument('--prompt-file',type=Path,help='Read your update instructions from Markdown')
    material = parser.add_mutually_exclusive_group()
    material.add_argument("--learning-material", dest="evidence", type=Path, help="Read-only LearningMaterial directory with README.md")
    material.add_argument("--evidence", dest="evidence", type=Path, help="Compatibility alias for --learning-material")
    parser.add_argument('--validation',type=Path,help='Optional read-only validation storage with README.md; no automatic evaluator')
    parser.add_argument('--goal',default='Improve the selected adapter using supplied LearningMaterial. Follow its evolution guide and report verified changes.')
    parser.add_argument('--timeout',type=int,default=240)
    parser.add_argument('--output',type=Path,help='Directory for evolution sessions (default: .demo/evolution inside this repo)')
    parser.add_argument('--model',help='Learner model id, overriding the client configuration (codex: -c model=...; Claude Code: --model); for an optimizer Evolver, its model (required)')
    parser.add_argument('--reasoning-effort',choices=['low','medium','high','xhigh'],help='Codex learner or optimizer-model reasoning effort; set it for reasoning models')
    parser.add_argument('--max-budget-usd',type=float,default=None,help='Claude Code only; default 3 USD there')
    parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--block',type=int,default=1,help='Caller-supplied session number')
    parser.add_argument('--tick',type=int,default=0,help='Caller-supplied current tick; zero means unknown')
    parser.add_argument('--config',type=Path,help='Framework execution policy: TOML, JSON, or YAML')
    parser.add_argument('--backend',choices=['codex','claude-code'])
    parser.add_argument('--workdir',type=Path,help='Read-only environment working directory; commands default here, writes still only target candidate/scratch')
    parser.add_argument('--deployment',type=Path,help='Deployment directory (venue texts for the method: contract/<slot>.md, delegation, host.json); copied read-only into the session')
    parser.add_argument('--notes',default='',help='Wrapper notes for this session (parked tools, brief overrun); rendered into the prompt by the method')
    parser.add_argument('--provider-name',help='Explicit OpenAI-compatible model provider for the Codex learner (with --provider-base-url and --provider-env-key)')
    parser.add_argument('--provider-base-url',help='OpenAI-compatible base URL (optimizer Evolvers default: $OPENAI_BASE_URL or OpenAI)')
    parser.add_argument('--provider-env-key',help='Environment variable holding the provider API key (optimizer Evolvers default: OPENAI_API_KEY)')
    parser.add_argument('--provider-wire-api',choices=['chat','responses'],default='chat')
    parser.add_argument('--bind',action='append',default=None,metavar='NAME=SPEC',help='Resource binding for the candidate adapter (search=URL, documents=URL, small_model=URL|model); repeatable. Enables the try_tool and small_model session tools.')
    parser.add_argument('--mount',action='append',default=None,metavar='NAME=PATH',help='Environment mount: the host directory or file PATH is readable at /environment/NAME (sandbox and broker); repeatable')
    reads=parser.add_mutually_exclusive_group()
    reads.add_argument('--read-dir',type=Path,action='append',default=None,help='Additional environment directory, read-only; repeatable')
    reads.add_argument('--read-all',action='store_true',help='Allow all filesystem reads; never expands write scope')
    net=parser.add_mutually_exclusive_group()
    net.add_argument('--network',dest='network',action='store_true',default=None)
    net.add_argument('--no-network',dest='network',action='store_false')
    search=parser.add_argument_group('optimizer Evolvers (gepa, skillopt, any kind = "optimizer"); --model, --reasoning-effort, --provider-* and --output apply too')
    search.add_argument('--environment',metavar='SPEC',help='module:factory or file.py:factory returning the Environment that scores candidates')
    search.add_argument('--environment-config',type=Path,help='YAML/JSON mapping passed to the factory')
    search.add_argument('--settings',type=Path,help='YAML/JSON mapping of Evolver settings (keys of its evolver.toml [settings])')
    search.add_argument('--set',action='append',default=[],metavar='KEY=VALUE',help='One Evolver setting; repeatable')
    search.add_argument('--component',action='append',default=None,help='Adapter text file to optimize; repeatable (default: the context file)')
    search.add_argument('--training-state',type=Path,help="Resume from a previous session's training_state/")
    search.add_argument('--max-tokens',type=int,default=32768,help='Output-token cap per model call')
    search.add_argument('--temperature',type=float,help='Sampling temperature for every model call (default: the provider\'s)')
    search.add_argument('--scripted-model',action='store_true',help='$0 toy run: a local scripted model instead of a provider')
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
    from harnessadapter.evolution.optimizer import is_optimizer, run_from_cli
    optimizer=is_optimizer(preset)
    blank=vars(parser.parse_args([]))
    other=[dest for dest in (OPTIMIZER_OPTIONS if not optimizer else LEARNER_OPTIONS) if getattr(args,dest)!=blank[dest]]
    if other:
        parser.error(f"{preset} is {'an optimizer' if optimizer else 'a learner'} Evolver; these options do not apply to it: "
                     + ', '.join('--'+{'evidence':'learning-material'}.get(d,d).replace('_','-') for d in other))
    if optimizer:
        if not args.environment:parser.error('An optimizer Evolver needs --environment')
        raise SystemExit(run_from_cli(parser,args,source,preset))
    if args.evidence is None:parser.error('This Evolver needs --learning-material')
    definition=EvolverDefinition(preset,prompt_override=prompt_override)
    policy=load_policy(args.config,backend=args.backend,read_paths=args.read_dir,read_all=args.read_all,network=args.network,defaults=definition.policy.serialize(),working_directory=args.workdir)
    kind='architecture' if args.architecture or (args.reference in BUILTIN_ARCHITECTURES and not args.adapter) else 'adapter'
    provider=None
    if args.provider_name or args.provider_base_url or args.provider_env_key:
        if not (args.provider_name and args.provider_base_url and args.provider_env_key):
            parser.error('--provider-name, --provider-base-url and --provider-env-key go together')
        provider={'name':args.provider_name,'base_url':args.provider_base_url,'env_key':args.provider_env_key,'wire_api':args.provider_wire_api}
    mounts={}
    for item in args.mount or []:
        name,sep,target=str(item).partition('=')
        if not sep or not name.strip():parser.error('--mount takes NAME=PATH')
        mounts[name.strip()]=target
    result=evolve(source,args.evidence,args.goal,args.timeout,args.max_budget_usd,args.prepare_only,args.block,args.tick,policy,definition,kind,validation_data=args.validation,output=args.output,model=args.model,reasoning_effort=args.reasoning_effort,bindings=args.bind,deployment=args.deployment,notes=args.notes,provider=provider,mounts=mounts)
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result['status'] in ('prepared','candidate_ready') else 1)


if __name__=='__main__':
    main()
