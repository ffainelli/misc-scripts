import importlib.util
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('runner', Path(__file__).with_name('run.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class PathTests(unittest.TestCase):
    def test_home_paths_are_expanded_before_preflight(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            (root / 'labgrid/labgrid').mkdir(parents=True)
            (root / 'labgrid/labgrid/__init__.py').touch()
            (root / 'labgrid/examples/boot-smoke').mkdir(parents=True)
            (root / 'labgrid/examples/boot-smoke/test_smoke.py').touch()
            (root / 'python').touch()
            output = root / 'buildroot/output/arm/board'
            output.mkdir(parents=True)
            (output / '.config').touch()
            cfg = dict(labgrid='~/labgrid', labgrid_python='~/python', labgrid_environments='~/environments',
                       buildroot='~/buildroot', seed='~/linux', state='~/state',
                       targets=[dict(build='arm/board')])
            with patch.dict(os.environ, HOME=home), patch.object(
                    runner, 'preflight', wraps=runner.preflight) as preflight:
                self.assertEqual(runner.run(cfg, SimpleNamespace(check=True)), 0)
            normalized = preflight.call_args.args[0]
            for key in ('labgrid', 'labgrid_python', 'labgrid_environments', 'buildroot', 'seed', 'state'):
                self.assertEqual(normalized[key], str(root / cfg[key][2:]))
                self.assertTrue(cfg[key].startswith('~/'))


if __name__ == '__main__':
    unittest.main()
