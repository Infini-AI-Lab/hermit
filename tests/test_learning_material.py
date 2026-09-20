"""Public LearningMaterial naming retains compatibility and read-only boundaries."""
from pathlib import Path
import contextlib
import io
import tempfile
import unittest
from unittest.mock import patch

from harnessadapter.adapter import Adapter, LearningMaterial, EvidenceStore
from harnessadapter.paths import ARCHITECTURES
from harnessadapter.adapter.resources import resolve_learning_material
from harnessadapter.evolution.launch import compile_launch
from harnessadapter.evolution.session import main, evolve
from harnessadapter.evolution.workspace import Workspace


class LearningMaterialTests(unittest.TestCase):
    def test_store_and_workspace_aliases_share_read_only_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'evidence').mkdir()
            (root/'evidence/README.md').write_text('Task, screenshots and user feedback.')
            self.assertIs(LearningMaterial, EvidenceStore)
            self.assertEqual(LearningMaterial(root/'evidence').list_records(), ['README.md'])
            material = LearningMaterial(root/'evidence')
            adapter = Adapter(ARCHITECTURES/'mwm', {'learning_material': material})
            self.assertIs(adapter.resources['evidence'], material)
            with self.assertRaises(ValueError):
                Adapter(ARCHITECTURES/'mwm', {'learning_material': material, 'evidence': LearningMaterial(root/'evidence')})
            workspace = Workspace(root)
            self.assertEqual(workspace.path('learning_material', 'README.md'), workspace.path('evidence', 'README.md'))
            for area in ('learning_material', 'evidence'):
                self.assertIn('feedback', workspace.read_file(area, 'README.md')['text'])
                with self.assertRaises(ValueError):
                    workspace.write_file(area, 'README.md', 'overwrite')
                with self.assertRaises(ValueError):
                    workspace.read_file(area, '../control/private')

    def test_yaml_aliases_and_ambiguous_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root/'launch.yaml'
            commands = []
            for name in ('learning_material', 'evidence'):
                config.write_text(f'action: evolve\narchitecture: mwm\n{name}: records\n')
                command = compile_launch(config)
                self.assertEqual(command[command.index('--learning-material')+1], str((root/'records').resolve()))
                commands.append(command)
            self.assertEqual(*commands)
            config.write_text('action: evolve\narchitecture: mwm\nlearning_material: records\nevidence: other\n')
            with self.assertRaises(ValueError):
                compile_launch(config)
            with self.assertRaises(ValueError):
                resolve_learning_material(root, root)
            with self.assertRaises(ValueError):
                evolve(root, evidence=root, learning_material=root)

    def test_cli_new_and_legacy_flags_select_same_input(self):
        for flag in ('--learning-material', '--evidence'):
            with patch('harnessadapter.evolution.session.evolve', return_value={'status':'prepared'}) as run:
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                    main(['--architecture','mwm',flag,'records','--prepare-only'])
                self.assertEqual(stopped.exception.code, 0)
                self.assertEqual(run.call_args.args[1], Path('records'))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            main(['--architecture','mwm','--learning-material','records','--evidence','other'])
        self.assertEqual(stopped.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
