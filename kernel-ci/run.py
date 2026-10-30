#!/usr/bin/env python3
"""Poll upstream master, build pinned sources, and test the home lab serially."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET


def command(argv, **kwargs):
    print('+ ' + ' '.join(map(str, argv)), flush=True)
    return subprocess.run(list(map(str, argv)), check=True, **kwargs)


def report(folder, sha, cases):
    (folder / 'summary.json').write_text(json.dumps(
        {'commit': sha, 'cases': cases}, indent=2) + '\n')
    suite = ET.Element('testsuite', name='kernel-lab', tests=str(len(cases)),
                       failures=str(sum(c['status'] == 'FAIL' for c in cases)))
    for case in cases:
        node = ET.SubElement(suite, 'testcase', classname='kernel-lab', name=case['name'])
        if case['status'] == 'FAIL':
            ET.SubElement(node, 'failure', message=case['detail']).text = case['detail']
    ET.ElementTree(suite).write(folder / 'junit.xml', encoding='utf-8', xml_declaration=True)


def preflight(cfg):
    lab = tomllib.loads(Path(cfg['lab']).read_text())
    if not Path(cfg['boot_script']).is_file():
        raise ValueError('Missing boot script')
    for item in cfg['targets']:
        output = Path(cfg['buildroot']) / 'output' / item['build']
        if not (output / '.config').is_file():
            raise ValueError(f'Missing Buildroot config: {output}')
        if 'target' in item:
            profile = lab['targets'][item['target']]['profiles'][item['mode']]
            if profile.get('image'):
                served = Path('/srv/tftp') / profile['image']
                if not served.exists():
                    raise ValueError(f'Missing TFTP image: {served}')
        print(json.dumps(item), flush=True)


def run(cfg, args):
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
        identity = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()
                                  + Path(cfg['lab']).read_bytes()
                                  + Path(cfg['boot_script']).read_bytes()
                                  + Path(__file__).read_bytes()
                                  + (Path(__file__).resolve().parents[1] / 'build-all.sh').read_bytes())
        for item in cfg['targets']:
            identity.update((Path(cfg['buildroot']) / 'output' / item['build'] / '.config').read_bytes())
        key = sha + ':' + identity.hexdigest()
        completed = state / 'completed'
        if not args.force and completed.exists() and completed.read_text().strip() == key:
            print(f'Already tested {sha}')
            return 0
        folder = args.output.resolve()
        folder.mkdir(parents=True, exist_ok=False)
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
            try:
                with (folder / f'{name}-build.log').open('w') as log:
                    command(['sh', builder], env=dict(env, TARGETS=item['build']),
                            stdout=log, stderr=subprocess.STDOUT)
                output = Path(cfg['buildroot']) / 'output' / item['build']
                (folder / f'{name}-buildroot.config').write_bytes((output / '.config').read_bytes())
                kernel = output / 'build/linux-custom'
                (folder / f'{name}-linux.config').write_bytes((kernel / '.config').read_bytes())
                if 'target' in item:
                    boots = folder / name
                    with (folder / f'{name}-boot.log').open('w') as log:
                        command([sys.executable, cfg['boot_script'], '--config', cfg['lab'],
                                 '--target', item['target'], '--mode', item['mode'],
                                 '--output', boots, '--live-log'], stdout=log, stderr=subprocess.STDOUT)
                    results = list(boots.rglob('results.json'))
                    if len(results) != 1:
                        raise ValueError('Expected exactly one boot result')
                    result = json.loads(results[0].read_text())
                    release = (kernel / 'include/config/kernel.release').read_text().strip()
                    uname = next(c['output'] for c in result['checks'] if c['name'] == 'uname')
                    if result['status'] != 'PASS' or marker not in release or release not in uname.split():
                        raise ValueError('Boot result failed or running kernel release differs from build')
                case.update(status='PASS', detail='build and boot passed' if 'target' in item else 'build only')
            except (OSError, ValueError, subprocess.CalledProcessError, KeyError, StopIteration) as exc:
                case['detail'] = str(exc)
            report(folder, sha, cases)
        # Failed completed runs are remembered; --force explicitly retries them.
        temporary = state / 'completed.tmp'
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
