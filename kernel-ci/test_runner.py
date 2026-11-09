import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('runner', Path(__file__).with_name('run.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def exercise(self, fail_build=False, stale=False, fail_test=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / 'state'
            (state / 'linux').mkdir(parents=True)
            labgrid = root / 'labgrid'
            (labgrid / 'labgrid').mkdir(parents=True)
            (labgrid / 'labgrid/__init__.py').write_text('local labgrid')
            suite = labgrid / 'examples/boot-smoke'
            suite.mkdir(parents=True)
            (suite / 'test_smoke.py').write_text('local pytest suite')
            environments = root / 'environments'
            environments.mkdir()
            (environments / 'board-arm.yaml').write_text('environment')
            python = root / 'python'
            python.touch()
            output = root / 'output/arm/board'
            output.mkdir(parents=True)
            (output / '.config').write_text('config')
            cfg = dict(state=str(state), buildroot=str(root), seed='seed', upstream='upstream',
                       labgrid=str(labgrid), labgrid_python=str(python),
                       labgrid_environments=str(environments),
                       targets=[dict(build='arm/board', target='board', mode='arm'),
                                dict(build='arm/board')])
            calls = []

            def command(argv, **kwargs):
                calls.append(argv)
                if argv[0] == 'sh':
                    if fail_build:
                        raise subprocess.CalledProcessError(1, argv)
                    kernel = output / 'build/linux-custom'
                    (kernel / 'include/config').mkdir(parents=True, exist_ok=True)
                    (kernel / '.config').write_text('kernel config')
                    (kernel / 'include/config/kernel.release').write_text('7.0-ci-' + 'a' * 12)
                if '--check' in argv:
                    return
                if '--target' in argv:
                    self.assertEqual(argv[0], str(python))
                    self.assertEqual(argv[argv.index('--environment') + 1], environments / 'board-arm.yaml')
                    folder = Path(argv[argv.index('--output') + 1])
                    folder.mkdir(parents=True)
                    release = 'old' if stale else '7.0-ci-' + 'a' * 12
                    (folder / 'results.json').write_text(json.dumps(dict(
                        status='FAIL' if fail_test else 'PASS', checks=[
                            dict(name='uname', output='Linux board ' + release + ' #1 arm',
                                 **{'pass': True}),
                            dict(name='ping', **{'pass': not fail_test})])))
                    if fail_test:
                        raise subprocess.CalledProcessError(1, argv)

            def capture(argv, **kwargs):
                return '' if 'status' in argv else 'a' * 40

            args = SimpleNamespace(check=False, force=False, output=root / 'results')
            with patch.object(runner, 'command', side_effect=command), patch.object(
                    runner.subprocess, 'check_output', side_effect=capture):
                result = runner.run(cfg, args)
                self.assertEqual(result, int(fail_build or stale or fail_test))
                self.assertEqual(sum(c[0] == 'sh' for c in calls), 2)
                self.assertEqual(any('--target' in c and '--check' not in c for c in calls), not fail_build)
                suite = runner.ET.parse(args.output / 'junit.xml').getroot()
                self.assertEqual(suite.attrib['tests'], '2')
                summary = (args.output / 'summary.txt').read_text()
                if fail_test:
                    self.assertIn('failed tests: ping', summary)
                    self.assertIn('1 passed, 1 failed', summary)
                    self.assertIn('failed tests: ping', suite.find('testcase/failure').text)
                elif fail_build:
                    self.assertIn('FAIL arm-board: build:', summary)
                elif stale:
                    self.assertIn('kernel release verification', summary)
                else:
                    self.assertIn('2 passed, 0 failed', summary)
                self.assertEqual(runner.run(cfg, args), 0)  # completed revisions deduplicate
                self.assertIn('no platforms run this poll',
                              (args.output / 'summary.txt').read_text())

    def test_success_and_deduplication(self):
        self.exercise()

    def test_build_failure_skips_boot_and_continues(self):
        self.exercise(fail_build=True)

    def test_stale_boot_fails(self):
        self.exercise(stale=True)

    def test_failed_boot_reports_test_names(self):
        self.exercise(fail_test=True)

    def test_local_labgrid_changes_invalidate_completed_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / 'labgrid'
            package = checkout / 'labgrid'
            package.mkdir(parents=True)
            driver = package / 'driver.py'
            driver.write_text('driver v1')
            suite = checkout / 'examples/boot-smoke'
            suite.mkdir(parents=True)
            test = suite / 'test_smoke.py'
            test.write_text('suite v1')
            env = root / 'env'
            env.mkdir()
            config = env / 'board-arm.yaml'
            config.write_text('config v1')
            output = root / 'output/arm/board'
            output.mkdir(parents=True)
            (output / '.config').write_text('build config')
            cfg = dict(labgrid=str(checkout), labgrid_environments=str(env),
                       buildroot=str(root), targets=[dict(build='arm/board', target='board', mode='arm')])
            for changed in [driver, test, config]:
                before = runner.configuration_identity(cfg)
                changed.write_text(changed.read_text() + ' modified')
                self.assertNotEqual(before, runner.configuration_identity(cfg))

    def test_summary_escapes_html(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            runner.report(folder, 'abc', [dict(name='board', status='FAIL',
                                             detail='boot: <bad> & broken')])
            self.assertIn('&lt;bad&gt; &amp; broken', (folder / 'summary.html').read_text())


if __name__ == '__main__':
    unittest.main()
