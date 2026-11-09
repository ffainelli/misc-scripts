#!/usr/bin/env python3
"""Poll upstream master, build pinned sources, and test the home lab serially."""
import argparse
import datetime
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


def command(argv, **kwargs):
    print('+ ' + ' '.join(map(str, argv)), flush=True)
    return subprocess.run(list(map(str, argv)), check=True, **kwargs)


def report(folder, sha, cases):
    (folder / 'summary.json').write_text(json.dumps(
        {'commit': sha, 'cases': cases}, indent=2) + '\n')
    lines = [f'Kernel {sha}',
             f"Platforms: {sum(c['status'] == 'PASS' for c in cases)} passed, "
             f"{sum(c['status'] == 'FAIL' for c in cases)} failed"]
    lines.extend(f"{c['status']} {c['name']}: {c['detail']}" for c in cases)
    write_summary(folder, '\n'.join(lines) + '\n')
    suite = ET.Element('testsuite', name='kernel-lab', tests=str(len(cases)),
                       failures=str(sum(c['status'] == 'FAIL' for c in cases)))
    for case in cases:
        node = ET.SubElement(suite, 'testcase', classname='kernel-lab', name=case['name'])
        if case['status'] == 'FAIL':
            ET.SubElement(node, 'failure', message=case['detail']).text = case['detail']
    ET.ElementTree(suite).write(folder / 'junit.xml', encoding='utf-8', xml_declaration=True)


def write_summary(folder, summary):
    (folder / 'summary.txt').write_text(summary)
    (folder / 'summary.html').write_text('<pre>' + html.escape(summary) + '</pre>')


def boot_failure(result):
    failures = [check['name'] + (': ' + check['error'] if check.get('error') else '')
                for check in result.get('checks', [])
                if check.get('pass') is False and check.get('outcome') != 'skipped']
    details = []
    if failures:
        details.append('failed tests: ' + ', '.join(failures))
    if result.get('failure'):
        details.append(result['failure'])
    if result.get('kernel_findings'):
        details.append('kernel diagnostics: ' + '; '.join(result['kernel_findings']))
    return '; '.join(details) or 'boot failed'


def environment_path(cfg, item):
    return Path(cfg['labgrid_environments']) / f"{item['target']}-{item['mode']}.yaml"


def labgrid_command(cfg, item):
    return [cfg['labgrid_python'], Path(__file__).with_name('labgrid_boot.py'),
            '--labgrid', cfg['labgrid'], '--environment', environment_path(cfg, item),
            '--target', item['target'], '--mode', item['mode']]


def preflight(cfg):
    for path in [Path(cfg['labgrid_python']), Path(cfg['labgrid']) / 'labgrid/__init__.py',
                 Path(cfg['labgrid']) / 'examples/boot-smoke/test_smoke.py']:
        if not path.is_file():
            raise ValueError(f'Missing local labgrid runtime: {path}')
    for item in cfg['targets']:
        output = Path(cfg['buildroot']) / 'output' / item['build']
        if not (output / '.config').is_file():
            raise ValueError(f'Missing Buildroot config: {output}')
        if 'target' in item:
            if not environment_path(cfg, item).is_file():
                raise ValueError(f'Missing labgrid environment: {environment_path(cfg, item)}')
            for image in item.get('tftp_images', []):
                served = Path(cfg.get('tftp_root', '/srv/tftp')) / image
                if not served.is_file():
                    raise ValueError(f'Missing TFTP image: {served}')
            command(labgrid_command(cfg, item) + ['--check'])
        print(json.dumps(item), flush=True)


def configuration_identity(cfg):
    identity = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode())
    for path in [Path(__file__), Path(__file__).with_name('labgrid_boot.py'),
                 Path(__file__).resolve().parents[1] / 'build-all.sh']:
        identity.update(path.read_bytes())
    checkout = Path(cfg['labgrid'])
    sources = list((checkout / 'labgrid').rglob('*.py'))
    sources += list((checkout / 'examples/boot-smoke').glob('*.py'))
    if (checkout / 'pyproject.toml').is_file():
        sources.append(checkout / 'pyproject.toml')
    for path in sorted(sources):
        identity.update(str(path.relative_to(checkout)).encode() + b'\0' + path.read_bytes())
    for item in cfg['targets']:
        identity.update((Path(cfg['buildroot']) / 'output' / item['build'] / '.config').read_bytes())
        if 'target' in item:
            identity.update(environment_path(cfg, item).read_bytes())
    return identity.hexdigest()


def run(cfg, args):
    cfg = dict(cfg)
    for key in ('labgrid', 'labgrid_python', 'labgrid_environments', 'buildroot', 'seed', 'state'):
        cfg[key] = str(Path(cfg[key]).expanduser())
    if 'tftp_root' in cfg:
        cfg['tftp_root'] = str(Path(cfg['tftp_root']).expanduser())
    preflight(cfg)
    if args.check:
        return 0
    state = Path(cfg['state']).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True)
    # Covers source checkout, shared output trees, TFTP images, and physical DUTs.
    with (state / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        source = state / 'linux'
        if not source.exists():
            command(['git', 'clone', '--reference-if-able', cfg['seed'],
                     cfg['upstream'], source])
        # Resume an initial clone created without a checkout (no index yet).
        if not (source / '.git/index').exists():
            command(['git', '-C', source, 'checkout', 'HEAD'])
        changes = subprocess.check_output(['git', '-C', source, 'status', '--porcelain'], text=True)
        if any(line != '?? localversion-ci' for line in changes.splitlines()):
            raise ValueError(f'CI source checkout has local changes: {source}')
        command(['git', '-C', source, 'fetch', '--no-tags', cfg['upstream'], 'master'])
        sha = subprocess.check_output(['git', '-C', source, 'rev-parse', 'FETCH_HEAD'], text=True).strip()
        # Include configuration in the deduplication key so changed coverage is rerun.
        key = sha + ':' + configuration_identity(cfg)
        completed = state / 'completed'
        completed_output = state / 'completed-output.json'
        if not args.force and completed.exists() and completed.read_text().strip() == key:
            print(f'Already tested {sha}')
            folder = args.output.resolve()
            folder.mkdir(parents=True, exist_ok=True)
            previous = json.loads(completed_output.read_text()) if completed_output.exists() else {}
            (folder / 'summary.json').write_text(json.dumps({
                'commit': sha, 'status': 'already tested',
                'previous_run': previous if previous.get('key') == key else None,
            }, indent=2) + '\n')
            if previous.get('key') == key:
                print('Previous results: ' + (previous.get('url') or previous['output']))
            summary = f'Already tested kernel {sha}; no platforms run this poll.\n'
            if previous.get('key') == key:
                summary += 'Previous results: ' + (previous.get('url') or previous['output']) + '\n'
            write_summary(folder, summary)
            return 0
        folder = args.output.resolve()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'config.json').write_text(json.dumps(cfg, indent=2) + '\n')
        cases = []
        report(folder, sha, cases)
        command(['git', '-C', source, 'checkout', '--detach', sha])
        # The boot must identify this revision even when LOCALVERSION_AUTO is off.
        marker = '-ci-' + sha[:12]
        (source / 'localversion-ci').write_text(marker + '\n')
        env = dict(os.environ, BR_DIR=cfg['buildroot'], LINUX_SOURCE=str(source))
        builder = Path(__file__).resolve().parents[1] / 'build-all.sh'
        for item in cfg['targets']:
            name = item['build'].replace('/', '-')
            case = {'name': name, 'status': 'FAIL', 'detail': ''}
            cases.append(case)
            phase = 'build'
            try:
                with (folder / f'{name}-build.log').open('w') as log:
                    command(['sh', builder], env=dict(env, TARGETS=item['build']),
                            stdout=log, stderr=subprocess.STDOUT)
                output = Path(cfg['buildroot']) / 'output' / item['build']
                (folder / f'{name}-buildroot.config').write_bytes((output / '.config').read_bytes())
                kernel = output / 'build/linux-custom'
                (folder / f'{name}-linux.config').write_bytes((kernel / '.config').read_bytes())
                if 'target' in item:
                    phase = 'boot'
                    boots = folder / name
                    boot_error = None
                    with (folder / f'{name}-boot.log').open('w') as log:
                        try:
                            command(labgrid_command(cfg, item) + ['--output', boots,
                                     '--timeout', str(cfg.get('boot_timeout', 600))],
                                    stdout=log, stderr=subprocess.STDOUT)
                        except subprocess.CalledProcessError as exc:
                            boot_error = exc
                    results = list(boots.rglob('results.json'))
                    if len(results) != 1:
                        raise ValueError(f'Expected exactly one boot result; {boot_error or "none found"}')
                    result = json.loads(results[0].read_text())
                    if result['status'] != 'PASS':
                        raise ValueError(boot_failure(result))
                    if boot_error:
                        raise boot_error
                    release = (kernel / 'include/config/kernel.release').read_text().strip()
                    uname = next(c['output'] for c in result['checks'] if c['name'] == 'uname')
                    if marker not in release or release not in uname.split():
                        raise ValueError('kernel release verification: running kernel release differs from build')
                case.update(status='PASS', detail='build and boot passed' if 'target' in item else 'build only')
            except (OSError, ValueError, subprocess.CalledProcessError, KeyError, StopIteration) as exc:
                case['detail'] = f'{phase}: {exc}'
            report(folder, sha, cases)
        # Failed completed runs are remembered; --force explicitly retries them.
        temporary = state / 'completed.tmp'
        completed_output.write_text(json.dumps({
            'key': key, 'output': str(folder),
            'url': os.environ.get('BUILD_URL', '') + 'artifact/'
                   if os.environ.get('BUILD_URL') else None,
        }, indent=2) + '\n')
        temporary.write_text(key + '\n')
        temporary.replace(completed)
        return int(any(c['status'] == 'FAIL' for c in cases))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('lab.json'))
    parser.add_argument('--output', type=Path, default=Path('kernel-ci-results') /
                        datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))
    parser.add_argument('--check', action='store_true', help='Read-only local preflight; no fetch/build/power')
    parser.add_argument('--force', action='store_true', help='Retry an already completed revision')
    args = parser.parse_args()
    try:
        return run(json.loads(args.config.read_text()), args)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
