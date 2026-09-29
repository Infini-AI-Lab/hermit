"""Architecture delivery and launch contract regression checks, without model calls."""
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
from harnessadapter.adapter.loader import Adapter
from harnessadapter.paths import ARCHITECTURES as ARCH
from harnessadapter.evolution.workspace import Workspace
from harnessadapter.evolution.launch import compile_launch
from harnessadapter.hosts.codex import prepare as codex_prepare
from harnessadapter.hosts.claude_code import prepare as claude_prepare


class ArchitectureTests(unittest.TestCase):
    def test_prompt_only_exports_and_removes_previous_generated_capabilities(self):
        for host, prepare, context_file in [('codex', codex_prepare, 'AGENTS.md'), ('claude-code', claude_prepare, 'CLAUDE.md')]:
            with self.subTest(host=host), tempfile.TemporaryDirectory() as d:
                output = Path(d)/'host'
                previous = prepare(ROOT/'examples/minimal_shop', output, sys.executable)
                self.assertTrue(previous['server'])
                result = prepare(ARCH/'prompt_only', output, sys.executable)
                self.assertIsNone(result['server'])
                self.assertIsNone(result['server_name'])
                self.assertIsNone(result['audit_log'])
                self.assertEqual(result['roles'], {})
                context = (output/context_file).read_text()
                for heading in ('# Adapter context', '## Procedure', '## Conventions', '## Lessons', '## Open questions'):
                    self.assertIn(heading, context)
                for role in previous['roles'].values():
                    self.assertFalse(Path(role).exists())
                if host == 'codex':
                    config = tomllib.loads((output/'.codex/config.toml').read_text())
                    self.assertNotIn('mcp_servers', config)
                    self.assertNotIn('agents', config)
                else:
                    self.assertEqual(json.loads((output/'.mcp.json').read_text())['mcpServers'], {})
                self.assertNotIn('wm(', (output/'PROMPT.md').read_text())
        core = Adapter(ARCH/'prompt_only')
        self.assertEqual(core.tools, {})
        self.assertEqual(core.agent_definitions(), [])

    def test_yaml_architecture_and_template_alias_and_relative_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); config = root/'launch.yaml'
            for field in ('architecture', 'template'):
                config.write_text(f'action: evolve\n{field}: prompt-only\nevidence: records\nprompt: Improve instructions\nnetwork: false\n')
                argv = compile_launch(config, prepare_only=True)
                self.assertEqual(argv[:3], ['evolve', '--architecture', 'prompt-only'])
                self.assertIn(str((root/'records').resolve()), argv)
                self.assertIn('--no-network', argv)
                self.assertIn('--prepare-only', argv)
            config.write_text('action: evolve\narchitecture: mwm\ntemplate: prompt-only\nevidence: records\n')
            with self.assertRaises(ValueError): compile_launch(config)
            config.write_text('action: deploy\nadapters: [mwm, prompt-only]\n')
            with self.assertRaises(ValueError): compile_launch(config)

    def test_optional_validation_is_absent_or_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with self.assertRaises(ValueError): Workspace(root).list_files('validation')
            (root/'validation').mkdir()
            (root/'validation/README.md').write_text('Validation instructions')
            workspace = Workspace(root)
            self.assertEqual(workspace.read_file('validation', 'README.md')['text'], 'Validation instructions')
            with self.assertRaises(ValueError): workspace.write_file('validation', 'README.md', 'changed')


if __name__ == '__main__': unittest.main()
