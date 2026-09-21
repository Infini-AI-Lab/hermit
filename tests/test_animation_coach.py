"""Check method isolation and real session preparation without model calls."""
from pathlib import Path
import shutil
import tempfile
import unittest

from harnessadapter.evolution.definition import EvolverDefinition
from harnessadapter.evolution.launch import compile_launch
from harnessadapter.paths import ARCHITECTURES


class AnimationCoachTests(unittest.TestCase):
    def test_distinct_contract_shared_maintenance_and_source_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('mwm', 'animation-coach'):
                session = root / name
                core = session / 'adapter'
                shutil.copytree(ARCHITECTURES / 'mwm', core)
                for area in ('control', 'environment', 'evidence'):
                    (session / area).mkdir()
                (core / 'meta/journal.md').write_text(
                    '# Journal\n' + ''.join(
                        f'\n## Session {i}\nDigest: observation {i}\n'
                        for i in range(1, 8)))
                definition = EvolverDefinition(name)
                state = definition.prepare_session(core, session, block=8, tick=8)
                self.assertIn('meta/journal.md', state['rolled'])
                self.assertTrue((core / 'meta/journal-archive.md').exists())
                prompt = definition.build_prompt(core, session / 'evidence', 'Learn animation strategies.', 'EVOLVER.md')
                self.assertIn('REVIEW SESSION #8', prompt)
                self.assertIn('world model: 0 memory entries', prompt)   # the index block appears only when memory has entries (native)
                contract = (session / 'method/CONTRACT.md').read_text()
                if name == 'animation-coach':
                    self.assertIn('Hypothesis / proposed strategy', contract)
                    self.assertNotIn('Advice about how some model should behave: no', contract)
                    self.assertFalse((session / 'method/skills').exists())
                    self.assertFalse((session / 'method/core.md').exists())
                    self.assertIn('Animation Coach runtime bridge', prompt)
                    self.assertFalse(definition.requires_validation)
                else:
                    self.assertIn('Advice about how some model should behave: no', contract)
                    self.assertTrue((session / 'method/skills/author-tool/SKILL.md').exists())

    def test_yaml_preserves_builtin_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.yaml'
            path.write_text('action: evolve\narchitecture: mwm\nevolver: animation-coach\nevidence: ./experience\n')
            command = compile_launch(path)
            self.assertEqual(command[command.index('--evolver') + 1], 'animation-coach')


if __name__ == '__main__':
    unittest.main()
