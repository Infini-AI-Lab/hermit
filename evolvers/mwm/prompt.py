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


def prepare_session(adapter, session, block=1, tick=0):
    material=session/'method';environment=session/'environment'
    shutil.copytree(HERE/'materials',material,dirs_exist_ok=True)
    core=_blocks((material/'core.md').read_text())
    slots={
        'domain_what':'This standalone deployment learns from supplied evidence and any environment access granted by the framework policy. No venue facts are pre-seeded.',
        'domain_read_exception':', the supplied evidence, method, environment, and history areas',
        'domain_write_dirs':'memory/, examples/, tools/, agents/, consumption/, meta/, and other core files (all core files are editable)',
        'env_consumption':'Hosts receive the original five-part MWM package and one wm(name,args) tool. Native roles are exported from SPEC.md.',
        'env_executable':'Learned tools use tools/<name>/{TOOL.md,run.py,test.py}. Verification uses the framework test_python capability; unavailable sandbox means no admission.',
        'env_verification':'The environment README declares readable directories, network policy, and available instruments. No benchmark-specific probe is implicitly provided.'}
    for key,value in slots.items():core=core.replace('{'+key+'}',value)
    # Control files no longer live inside the core. Preserve the knowledge-method
    # text while explicitly replacing only the old ownership paragraph.
    start=core.index('- THREE RULES.')
    end=core.index('## Layout',start)
    core=core[:start]+'''- Execution boundary: read supplied adapter, evidence, method, environment, and history material only. Write only the candidate adapter or scratch. Original evidence, method instructions, framework code, control state, and snapshots are read-only or not exposed. All adapter core files may change. There is no learner-writable .git or state.json controller file inside this core.
- Method conventions below guide MWM. The framework does not make its directory layout universal. A candidate must retain a valid public export contract; validity does not establish performance.

'''+core[end:]
    core=core[:core.index('## Session flow')]
    (material/'CONTRACT.md').write_text(core+'\n'+_blocks((material/'agenda.md').read_text())+
        '\nThe standalone execution boundary and virtual path mapping are described in TRANSPORT.md.\n')
    shutil.copyfile(HERE/'METHOD.md',material/'TRANSPORT.md')
    (environment/'README.md').write_text('# Environment access\n\nThis run supplies offline evidence only. No SQL, browser, environment-action, or scoring tool is bound. Do not infer absent capabilities from the source research setup.\n')
    journal,docket=_modules()
    rolled,notice=journal.roll_with_notice(str(adapter))
    capped=docket.enforce_case_caps(str(adapter))
    state={'block':block,'tick':tick,'rolled':rolled,'capped':capped,'roll_notice':notice,'docket':docket.render_status(str(adapter))}
    (session/'control/mwm-status.json').write_text(json.dumps(state,indent=2)+'\n')
    return state


def build_prompt(adapter: Path, evidence: Path, goal: str):
    session=adapter.parent;state=json.loads((session/'control/mwm-status.json').read_text())
    entries=[]
    files=[p for p in (adapter/'memory').glob('*.md') if p.name!='README.md' and not p.name.startswith('_')]
    for p in sorted(files,key=lambda p:p.stat().st_mtime_ns,reverse=True)[:100]:
        fields=dict(re.findall(r'^(brief|last_confirmed_t):\s*(.*)$',p.read_text(),re.M))
        stamp=fields.get('last_confirmed_t','?');age=''
        if stamp.isdigit() and state['tick'] and state['tick']-int(stamp)>0.4*state['tick']:
            age=f" (unverified for {state['tick']-int(stamp)} ticks)"
        entries.append(f"[{p.stem}] t={stamp}{age} · {fields.get('brief','(no brief)')[:300]}")
    brief=adapter/'consumption/brief.md'
    return '\n\n'.join([(HERE/'METHOD.md').read_text(),f"REVIEW SESSION #{state['block']}.\n"+goal,
        'Read method/CONTRACT.md fully, then environment/README.md and evidence/README.md. Read meta/journal.md and the relevant docket cases. Use the pinned skills and patterns when the agenda calls for them.',
        '[Memory index]\n'+('\n'.join(entries) or '(empty)'),state['docket'],
        f"consumption/brief.md: {len(brief.read_text()) if brief.exists() else 0}/30000 chars",state['roll_notice'],
        'Raw evidence keeps its supplied format. The framework computes no outcome statistics from it. Follow its README for addresses.'])


def finalize(adapter: Path, session: Path):
    # Admission policy is method-specific; the execution primitive is framework-owned.
    from python_sandbox import PythonSandbox
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
    result={'backend':sandbox.status(),'tools':reports}
    (session/'control/admission.json').write_text(json.dumps(result,indent=2)+'\n')
    return result
