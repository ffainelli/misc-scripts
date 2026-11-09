#!/usr/bin/env python3
"""Run the local labgrid pytest suite and normalize its results for kernel CI."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET


def validate_environment(args):
    sys.path.insert(0, str(args.labgrid))
    import labgrid
    import labgrid.pytestplugin  # verify runtime dependencies before building
    import pytest
    import telnetlib3  # WTI power control still uses Telnet

    package = Path(labgrid.__file__).resolve()
    if not package.is_relative_to(args.labgrid):
        raise ValueError(f'Labgrid was imported from {package}, not {args.labgrid}')
    suite = args.labgrid / 'examples/boot-smoke/test_smoke.py'
    if not suite.is_file():
        raise ValueError(f'Missing labgrid pytest suite: {suite}')
    env = labgrid.Environment(str(args.environment))
    try:
        smoke = env.config.get_option('smoke')
        if smoke['target'] != args.target or smoke['profile'] != args.mode:
            raise ValueError('Labgrid environment target/profile does not match CI configuration')
        if not any(check['name'] == 'uname' for check in smoke['checks']):
            raise ValueError('Labgrid smoke checks must include uname for kernel release verification')
        target = env.get_target()
        if target is None or target.get_strategy() is None:
            raise ValueError('Labgrid environment must define a main target and strategy')
        if smoke.get('rpi_config') and not Path(smoke['rpi_config']).expanduser().is_file():
            raise ValueError(f'Missing Raspberry Pi boot configuration: {smoke["rpi_config"]}')
    finally:
        env.cleanup()
    return dict(python=sys.executable, python_version=sys.version.split()[0],
                package=str(package), pytest=pytest.__version__,
                environment=str(args.environment), suite=str(suite),
                checks=[check['name'] for check in smoke['checks']] + ['kernel_log'])


def failure_message(node):
    message = node.get('message') or (node.text or '').strip() or node.tag
    if 'StrategyError' in message and node.text:
        # Pytest wraps setup failures; retain the underlying exception for the
        # platform summary instead of only reporting "strategy is broken".
        causes = re.findall(r'^E\s+(.+)', node.text, re.MULTILINE)
        meaningful = [line for line in causes if 'StrategyError' not in line
                      and ('Error:' in line or 'TIMEOUT:' in line or 'Exception:' in line)]
        if meaningful:
            message = meaningful[0]
    return message[:4000]


def read_results(filename, exit_code, expected_checks=()):
    if not filename.is_file():
        return dict(status='FAIL', checks=[], kernel_findings=[],
                    failure=f'Labgrid pytest produced no JUnit report (exit {exit_code})')
    try:
        root = ET.parse(filename).getroot()
    except ET.ParseError as exc:
        return dict(status='FAIL', checks=[], kernel_findings=[], failure=f'Invalid labgrid JUnit report: {exc}')
    checks = []
    findings = []
    for case in root.iter('testcase'):
        name = case.get('name', '<unnamed>')
        if name.startswith('test_check[') and name.endswith(']'):
            name = name[len('test_check['):-1]
        elif name == 'test_kernel_log':
            name = 'kernel_log'
        properties = {p.get('name'): p.get('value', '') for p in case.findall('properties/property')}
        problem = next((child for child in case if child.tag in ('failure', 'error', 'skipped')), None)
        check = dict(name=name, output=properties.get('output', ''), **{'pass': problem is None})
        if properties.get('command'):
            check['command'] = properties['command']
        if problem is not None:
            check['error'] = failure_message(problem)
            check['outcome'] = problem.tag
            if name == 'kernel_log' and problem.tag != 'skipped':
                for line in check['error'].splitlines():
                    line = line.removeprefix('AssertionError: ').strip()
                    if line.startswith('assert '):
                        break
                    if line:
                        findings.append(line)
        checks.append(check)
    failed = [check['name'] for check in checks
              if not check['pass'] and check.get('outcome') != 'skipped']
    skipped = [check['name'] for check in checks if check.get('outcome') == 'skipped']
    missing = set(expected_checks) - {check['name'] for check in checks if check['pass']}
    passed_uname = any(check['name'] == 'uname' and check['pass'] and check['output'] for check in checks)
    success = exit_code == 0 and bool(checks) and not failed and not skipped and not missing and passed_uname
    failure = None
    if failed:
        failure = 'Labgrid pytest failed tests: ' + ', '.join(failed)
    elif skipped:
        failure = 'Labgrid pytest skipped tests: ' + ', '.join(skipped)
    elif missing:
        failure = 'Labgrid pytest did not pass checks: ' + ', '.join(sorted(missing))
    elif not passed_uname:
        failure = 'Labgrid pytest did not capture a successful uname check'
    elif exit_code:
        failure = f'Labgrid pytest exited with status {exit_code}'
    return dict(status='PASS' if success else 'FAIL', checks=checks,
                kernel_findings=findings, failure=failure)


def run_suite(args):
    runtime = validate_environment(args)
    if args.check:
        print(json.dumps(runtime), flush=True)
        return 0
    args.output.mkdir(parents=True, exist_ok=True)
    junit = args.output / 'pytest-junit.xml'
    # A crashed invocation must not reuse a report from a previous attempt.
    junit.unlink(missing_ok=True)
    (args.output / 'results.json').unlink(missing_ok=True)
    child_env = dict(os.environ, PYTHONPATH=str(args.labgrid))
    child_env.pop('PYTEST_ADDOPTS', None)
    if 'LG_LOGIN_PASSWORD' not in child_env and 'BOOTSMOKE_LOGIN_PASSWORD' in child_env:
        child_env['LG_LOGIN_PASSWORD'] = child_env['BOOTSMOKE_LOGIN_PASSWORD']
    argv = [sys.executable, '-m', 'pytest', '-p', 'labgrid', '-o', 'addopts=',
            '-o', 'junit_family=xunit1', '--lg-env', str(args.environment),
            '--lg-log', str(args.output), '--junitxml=' + str(junit),
            '--junit-prefix=' + args.target + '.' + args.mode, '-vv', 'test_smoke.py']
    print('+ ' + ' '.join(argv), flush=True)
    try:
        outcome = subprocess.run(argv, cwd=Path(runtime['suite']).parent,
                                 env=child_env, timeout=args.timeout)
        code = outcome.returncode
    except subprocess.TimeoutExpired:
        print(f'Labgrid pytest timed out after {args.timeout} seconds', flush=True)
        code = 124
    result = read_results(junit, code, runtime['checks'])
    kernel = args.output / 'kernel.log'
    if kernel.is_file():
        panics = re.findall(r'Kernel panic[^\r\n]*', kernel.read_text(errors='replace'))
        if panics:
            result.update(status='FAIL', failure='Test kernel panicked')
            result['kernel_findings'] = list(dict.fromkeys(result['kernel_findings'] + panics))
    result.update(target=args.target, mode=args.mode, pytest_exit_code=code, labgrid=runtime)
    (args.output / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    return int(result['status'] != 'PASS')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--labgrid', type=Path, required=True)
    parser.add_argument('--environment', type=Path, required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--mode', required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--check', action='store_true', help='Validate imports/configuration without activating hardware')
    args = parser.parse_args()
    if not args.check and args.output is None:
        parser.error('--output is required unless --check is used')
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    for name in ('labgrid', 'environment', 'output'):
        if getattr(args, name) is not None:
            setattr(args, name, getattr(args, name).expanduser().resolve())
    try:
        return run_suite(args)
    except (ImportError, OSError, ValueError, KeyError) as exc:
        print(f'Labgrid setup failed: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
