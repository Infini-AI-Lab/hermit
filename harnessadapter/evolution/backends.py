"""Codex and Claude Code as learner clients of the same constrained tool broker."""
import json
import os
from pathlib import Path
import shutil
import tomllib

from harnessadapter.policy.execution import PolicySandbox, toml_value


def command_for(session, policy, mcp_config, budget, model=None, reasoning_effort=None, provider=None):
    if not shutil.which('codex'):
        raise RuntimeError('The shared sandbox executor requires an installed Codex CLI')
    if reasoning_effort is not None and reasoning_effort not in ('low','medium','high','xhigh'):
        raise ValueError('Unsupported reasoning effort')
    if policy.backend=='claude-code':
        if reasoning_effort is not None:
            raise ValueError('--reasoning-effort currently applies to the Codex backend only')
        if not shutil.which('claude'):raise RuntimeError('Claude Code is not installed')
        return ['claude','-p','--output-format','stream-json','--verbose','--tools','',
                '--disable-slash-commands','--strict-mcp-config','--mcp-config',str(mcp_config),
                '--allowedTools','mcp__evolution__*','--permission-mode','dontAsk','--permission-prompts','none',
                '--settings',json.dumps({'disableAllHooks':True,'autoMemoryEnabled':False,'claudeMdExcludes':['**']}),
                '--max-budget-usd',str(budget)]+(['--model',model] if model else [])
    # Native commands are disabled. The read-only native profile is defense in
    # depth for any remaining native file operation; actual edits use the broker.
    command=['codex','--strict-config','--ask-for-approval','never','exec','--ignore-user-config','--ignore-rules',
             '--skip-git-repo-check','--ephemeral','--json','--cd',str(session/'host'),
             '--output-last-message',str(session/'control/report.md')]
    def setting(key,value):command.extend(['-c',key+'='+toml_value(value)])
    config=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))/'config.toml'
    values=tomllib.loads(config.read_text()) if config.exists() else {}
    if provider:
        # An explicit OpenAI-compatible provider (base_url + key env var): the
        # launcher may point it at a relay/meter; nothing else is inferred.
        name=provider['name']
        setting('model_provider',name)
        setting(f'model_providers.{name}.name',name)
        setting(f'model_providers.{name}.base_url',provider['base_url'])
        setting(f'model_providers.{name}.env_key',provider['env_key'])
        setting(f'model_providers.{name}.wire_api',provider.get('wire_api','chat'))
        if provider.get('requires_openai_auth') is not None:
            setting(f'model_providers.{name}.requires_openai_auth',bool(provider['requires_openai_auth']))
    elif values.get('model_provider','openai')!='openai':
        raise RuntimeError('This Codex backend supports the existing OpenAI login, or an explicit --provider-* set; the client configuration names another provider')
    for key in ('model','model_reasoning_effort'):
        if key in values:setting(key,values[key])
    if reasoning_effort is not None:setting('model_reasoning_effort',reasoning_effort)
    if model:setting('model',model)   # an explicit choice outranks the client configuration
    setting('default_permissions','harnessadapter_client')
    setting('permissions.harnessadapter_client',PolicySandbox(session,policy).profile(network=False,writable=False))
    for feature in ('shell_tool','unified_exec','shell_snapshot','multi_agent','apps','plugins','remote_plugin','image_generation','view_image'):
        setting('features.'+feature,False)
    setting('agents.enabled',False);setting('web_search','disabled')
    server=json.loads(mcp_config.read_text())['mcpServers']['evolution']
    for key in ('command','args'):
        setting('mcp_servers.evolution.'+key,server[key])
    setting('mcp_servers.evolution.required',True)
    setting('mcp_servers.evolution.tool_timeout_sec',120)
    # The user authorized these framework capabilities. Their OS policy remains
    # mandatory; this is not approval of unrelated MCP or native tools.
    setting('mcp_servers.evolution.default_tools_approval_mode','approve')
    command.append('-')
    return command


def codex_turn_outcome(events):
    """A completed turn can recover from earlier transport error notifications."""
    terminal=[(i,e) for i,e in enumerate(events) if e.get('type') in ('turn.completed','turn.failed')]
    if not terminal:
        return {'subtype':'error','is_error':True}
    index,event=terminal[-1]
    success=event.get('type')=='turn.completed' and not any(e.get('type')=='error' for e in events[index+1:])
    return {'subtype':'success' if success else 'error','is_error':not success}
