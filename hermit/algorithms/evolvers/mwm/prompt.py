"""Original MWM contract/materials and method-specific maintenance, adapted for file capabilities."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys
import types
import uuid

HERE = Path(__file__).resolve().parent


def _modules():
    name='_mwm_maintenance_'+uuid.uuid4().hex
    package=types.ModuleType(name);package.__path__=[str(HERE/'maintenance')];sys.modules[name]=package
    import importlib
    docket=importlib.import_module(name+'.docket')
    hypothesis=importlib.import_module(name+'.hypothesis')
    # Pin the source's V2 default, independent of unrelated shell ablation variables.
    hypothesis.ENABLED=False;docket.ENABLED=True
    return importlib.import_module(name+'.journal'),docket


def _gates():
    """The ported admission gates, loaded inside a maintenance package so its
    relative imports (hypothesis) resolve; hypothesis stays at the V2 default."""
    name='_mwm_maintenance_'+uuid.uuid4().hex
    package=types.ModuleType(name);package.__path__=[str(HERE/'maintenance')];sys.modules[name]=package
    import importlib
    hypothesis=importlib.import_module(name+'.hypothesis');hypothesis.ENABLED=False
    return importlib.import_module(name+'.gates')


def _blocks(text):
    selected={'HYP':False,'NOHYP':True,'DOCKET':True};stack=[];lines=[]
    for line in text.splitlines():
        match=re.fullmatch(r'<!-- (HYP|NOHYP|DOCKET):(BEGIN|END) -->',line.strip())
        if match:
            if match[2]=='BEGIN':stack.append(selected[match[1]])
            elif stack:stack.pop()
            continue
        if all(stack):lines.append(line)
    return '\n'.join(lines)+'\n'


SLOT_NAMES=('domain_what','domain_read_exception','domain_write_dirs','env_consumption','env_executable','env_verification')
GENERIC_SLOTS={
    'domain_what':'This standalone deployment learns from supplied evidence and any environment access granted by the framework policy. No venue facts are pre-seeded.',
    'domain_read_exception':', EXCEPT the read-only\n       mounts the harness gives you: /patterns (the pattern\n       library), /skills (your skill library), /evidence (the raw\n       record store, when present) and /environment (what you can\n       reach of the environment — read its README.md first)',   # the method's own (deployment.METHOD_SLOTS)
    'domain_write_dirs':'memory/, examples/, consumption/, meta/,\n       tools/, agents/, bin/',
    'env_consumption':'Hosts receive the original five-part MWM package and one wm(name,args) tool. Native roles are exported from SPEC.md.',
    'env_executable':'Learned tools use tools/<name>/{TOOL.md,run.py,test.py}; `captest --wrepo . --name <name>` runs a tool\'s test in the admission sandbox; an unavailable sandbox means no admission.',
    'env_verification':'The environment README declares readable directories, network policy, and available instruments. No benchmark-specific probe is implicitly provided.'}
CAPTEST_CMD='captest'   # the native command; evolvers/mwm/bin/captest is on the learner's PATH
NATIVE_PROMPT=True      # the session prompt is the native review/bootstrap prompt; the framework adds no opener or policy block
# The architecture's core code is not the learner's to edit — native metaworld
# keeps wm_iface/wm_tools in the frozen package, outside the world model. The
# broker refuses writes here and a session that changed any of them by another
# route is not adopted (session.finalize: unchanged.protected).
PROTECTED_PATHS=('adapter.json','interface.py','mwm_core','CONTRACT.md','EVOLVER.md')
BOOTSTRAP_PROMPT=(
    "BOOTSTRAP SESSION (before any episodes). Read CONTRACT.md fully, "
    "then /environment/README.md. Where it lists public information, "
    "read it and seed memory/ with what that information states, each "
    "entry with its evidence extent (derived_from: the source path; "
    "nothing graded yet) — none of it has been verified against the "
    "environment, so say so in each entry's text"
    " — each entry naming the case shape that would test it, "
    "with the stated value as the working default. "
    "Where it lists none, memory/ starts empty. Build an initial "
    "consumption/brief.md that honestly states what is unverified "
    "— or that nothing is known yet"
    " — and record your open questions in meta/cases/.")


def _adapter_module(adapter: Path, name: str):
    """A module of the candidate's own mwm_core (the code that will serve)."""
    package='_mwm_core_'+hashlib.sha256(str(adapter).encode()).hexdigest()[:12]
    if package not in sys.modules:
        pkg=types.ModuleType(package);pkg.__path__=[str(adapter/'mwm_core')];sys.modules[package]=pkg
    full=package+'.'+name
    if full in sys.modules:return sys.modules[full]
    spec=importlib.util.spec_from_file_location(full,adapter/'mwm_core'/(name+'.py'))
    module=importlib.util.module_from_spec(spec);sys.modules[full]=module;spec.loader.exec_module(module)
    return module


def _deployment(session: Path) -> dict:
    """The deployment directory's contents, when the session carries one (the method's defaults fill what the
    venue leaves out; see deployment.py)."""
    root=session/'deployment'
    if not root.is_dir():return {}
    from hermit.algorithms.evolvers.mwm import deployment as _dep
    return _dep.load(root)


def _wm_counts(adapter: Path) -> str:
    mdir=adapter/'memory'
    n_mem=sum(1 for f in mdir.glob('*.md') if not f.name.startswith('_') and f.name!='README.md') if mdir.is_dir() else 0
    troot=adapter/'tools'
    n_tools=sum(1 for d in troot.iterdir() if d.is_dir() and not d.name.startswith(('_','.')) and (d/'run.py').is_file()) if troot.is_dir() else 0
    return f"world model: {n_mem} memory entries, {n_tools} admitted tools"


def prepare_session(adapter, session, block=1, tick=0, method_root=None):
    method_root = Path(method_root) if method_root is not None else HERE
    material=session/'method'
    deployment=_deployment(session)
    if method_root == HERE:
        shutil.copytree(HERE/'materials',material,dirs_exist_ok=True)
        core=_blocks((material/'core.md').read_text())
        slots=dict(GENERIC_SLOTS)
        for name in SLOT_NAMES:
            if name in deployment.get('slots',{}):slots[name]=deployment['slots'][name]
        # The sub-agent sentence of "This environment" comes from the
        # deployment's delegation mode, as in the native renderer.
        from importlib import import_module  # noqa: F401
        delegation=_adapter_module(Path(adapter),'delegation')
        slots['env_executable']=(slots['env_executable'].rstrip()+'\n\n'+delegation.sentence(deployment.get('delegation','native'))).strip()+'\n'
        for key,value in slots.items():core=core.replace('{'+key+'}',value)
        core=core.replace('@CAPTEST@',CAPTEST_CMD)
        # The native THREE RULES paragraph stays; only what rules 2 and 3 name
        # changes to the files this transport actually protects: the wrapper's
        # files here are CONTRACT.md and EVOLVER.md (no AGENTS.md/CLAUDE.md/env/),
        # and instead of state.json the learner must not edit the code that
        # serves the world model (PROTECTED_PATHS; the broker refuses the
        # write, finalize discards a session that changed them another way).
        rule2_native="    2. Do not write the raw record (evidence/), env/, .git/, or the\n       wrapper's files (CONTRACT.md, AGENTS.md, CLAUDE.md). The standard\n       layout for world-model artifacts is "
        rule2_here="    2. Do not write the raw record (evidence/), .git/, or the wrapper's\n       files (CONTRACT.md, EVOLVER.md). The standard layout for\n       world-model artifacts is "
        rule3_native='    3. Do not edit state.json.\n'
        rule3_here='    3. Do not edit the code that serves the world model to hosts:\n       adapter.yaml, interface.py, check.py, mwm_core/.\n'
        if rule2_native not in core or rule3_native not in core:
            raise ValueError('materials/core.md no longer carries the native THREE RULES wording')
        core=core.replace(rule2_native,rule2_here).replace(rule3_native,rule3_here)
        # As the native renderer: the agenda replaces the "## Session flow"
        # stub wholesale, and the contract's closing rules (no editing of
        # evidence/, no git, no network, the turn limit) stay after it.
        a=core.index('## Session flow')
        b=core.index('Do NOT edit evidence/')
        core=core[:a]+_blocks((material/'agenda.md').read_text())+'\n'+core[b:]
        contract=core.rstrip('\n')+'\n'
        (material/'CONTRACT.md').write_text(contract)
        # Native layout: the contract sits at the repository root, read-only for
        # the learner (the workspace refuses writes to it); the transport notes
        # live beside it in the method area for the learner to consult if needed.
        (Path(adapter)/'CONTRACT.md').write_text(contract)
    else:
        # A method can reuse MWM maintenance without inheriting its learning objective.
        shutil.copytree(method_root/'materials', material, dirs_exist_ok=True)
        if not (material/'CONTRACT.md').is_file():
            raise ValueError('An MWM lifecycle variant must supply materials/CONTRACT.md')
        shutil.copyfile(method_root/'METHOD.md', material/'TRANSPORT.md')
    # The environment README is the framework's (bindings, policy); the method adds nothing.
    journal,docket=_modules()
    rolled,notice=journal.roll_with_notice(str(adapter))
    capped=docket.enforce_case_caps(str(adapter))
    try:advice=_gates().check(str(adapter))
    except Exception as exc:  # noqa: BLE001
        advice=[f'gate check unavailable: {exc}']
    try:index=_adapter_module(Path(adapter),'wm_tools').index(str(adapter),now_t=tick)
    except Exception as exc:  # noqa: BLE001
        index=f'(memory index unavailable: {exc})'
    state={'block':block,'tick':tick,'rolled':rolled,'capped':capped,'roll_notice':notice,
           'docket':docket.render_status(str(adapter)),'advice':advice,'index':index,
           'wm_counts':_wm_counts(Path(adapter)),'deployment':bool(deployment),'delegation':deployment.get('delegation','native')}
    (session/'control/mwm-status.json').write_text(json.dumps(state,indent=2)+'\n')
    return state


def _status_block(adapter: Path, state: dict) -> str:
    brief=adapter/'consumption/brief.md'
    if brief.exists():
        n=len(brief.read_text())
        bline=f"consumption/brief.md: {n}/30000 chars"+(" OVER BUDGET — the hosts of the block just played received it TRUNCATED at 30000 (visible seam at the cut); trim it now" if n>30000 else "")
    else:
        bline="consumption/brief.md: (missing)"
    index=state.get('index') or ''
    index_block=("[Memory index] one line per entry — [id] t=<its last_confirmed_t> · brief — newest edit first, the order and brief the hosts see:\n"+index) if index else ''
    cases=state.get('docket') or ''
    return ("\n\n[Wrapper-computed status]\n"+((cases+"\n") if cases else "")
            +(("\n\n"+index_block) if index_block else "")
            +"\n"+state.get('wm_counts','')+"\n"+bline
            +(("\n"+state['roll_notice']) if state.get('roll_notice') else ""))


def _render_advice(advice):
    """Native gates.render_advice wording (mwm/gates.py)."""
    advice=[a for a in (advice or []) if a]
    if not advice:
        return "Repository check on your last session: nothing to report."
    head=advice[:12]; more=len(advice)-len(head)
    lines=["Repository check on your last session — findings, not verdicts; agenda item 1 is where they belong:"]
    lines+=[f"  - {a}" for a in head]
    if more:lines.append(f"  ... and {more} more of the same kinds.")
    return "\n".join(lines)


def build_prompt(adapter: Path, evidence: Path, goal: str, method_root=None, notes: str = '', guide: str = 'EVOLVER.md', session=None):
    """The native session prompt (mwm/session.py review_prompt + run_session):
    the contract pointer, the block's evidence listing, wrapper notes, the
    previous check's advice, the agenda sentence, and the wrapper-computed
    status block. Nothing framework-specific is added here."""
    session=Path(session) if session is not None else adapter.parent;state=json.loads((session/'control/mwm-status.json').read_text())
    block=int(state.get('block',1))
    if block==0:
        body=BOOTSTRAP_PROMPT
    else:
        parts=[f"REVIEW SESSION #{block}.",
               "Below is what the hosts did in this block, one entry per episode; the complete records are at the addresses given (/evidence/README.md describes them).",
               goal]
        if notes and notes.strip():parts.append("Notes from the wrapper:\n"+notes.strip())
        parts.append(_render_advice(state.get('advice')))
        parts.append("Follow the session agenda in CONTRACT.md.")
        body="\n\n".join(parts)
    body=("Read ./CONTRACT.md first — it is the operating contract for this repository and you are judged against it.\n\n"+body)
    method_root = Path(method_root) if method_root is not None else HERE
    if method_root != HERE:
        # A lifecycle variant with its own objective keeps its runtime bridge in front.
        body=(method_root/'METHOD.md').read_text()+"\n\n"+body
    return body+_status_block(adapter,state)


def finalize(adapter: Path, session: Path):
    # Admission policy is method-specific; the execution primitive is framework-owned.
    from hermit.runtime.python_sandbox import PythonSandbox
    sandbox=PythonSandbox(adapter);reports=[]
    for run in sorted((adapter/'tools').glob('*/run.py')):
        name=run.parent.name
        if name.startswith(('_','.')):continue
        result=sandbox.test(str(run.parent.relative_to(adapter)))
        record={'tool':name,**result}
        if result['available'] and not result['passed']:
            destination=adapter/'tools/_rejected'/name
            if destination.exists():destination=destination.with_name(name+'-'+uuid.uuid4().hex[:8])
            destination.parent.mkdir(parents=True,exist_ok=True);run.parent.rename(destination)
            record['parked_at']=str(destination.relative_to(adapter))
        reports.append(record)
    # Admission gates run on the finished candidate, as the native wrapper runs
    # them after every session; findings make the session needs_review.
    try:
        findings=_gates().check(str(adapter))
    except Exception as exc:  # noqa: BLE001
        findings=[f'gate check failed to run: {exc}']
    result={'backend':sandbox.status(),'tools':reports,'gates':findings}
    (session/'control/admission.json').write_text(json.dumps(result,indent=2)+'\n')
    return result
