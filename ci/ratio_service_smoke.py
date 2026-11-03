"""Exercise the legacy systemd read-only sandbox using isolated fake NIC state."""
import errno
import grp
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import uuid


def worker(root, expected):
    from vps_toolkit import ratio, traffic
    from vps_toolkit.core import Context, defaults
    ctx = Context(root)
    item = defaults()['traffic']
    item.update(strategy='ratio')
    try:
        with traffic.engine.locked(ctx.path(item['state_dir'])):
            result = ratio.sample(ctx, item)
    except OSError as exc:
        if expected == 'blocked' and exc.errno == errno.EROFS:
            print('Legacy read-only sandbox reproduced: PASS')
            return
        raise
    assert expected == 'writable', 'old sandbox unexpectedly allowed meter writes'
    assert result['upload_bytes'] == 5_000_000_000
    assert result['download_bytes'] == 6_000_000_000
    assert result['needed_bytes'] == 4_000_000_000
    print('Legacy sandbox with scoped meter permission: PASS')


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        return worker(sys.argv[2], sys.argv[3])
    assert os.geteuid() == 0, 'requires isolated Linux CI root'
    source = Path(__file__).resolve().parents[1]
    account = pwd.getpwnam('nobody')
    group = grp.getgrgid(account.pw_gid).gr_name
    with tempfile.TemporaryDirectory(prefix='vps-toolkit-ratio-', dir='/run') as temporary:
        base = Path(temporary)
        os.chmod(base, 0o755)
        package = base / 'source'
        package.mkdir()
        for name in ('vps_toolkit', 'compat'):
            shutil.copytree(source / name, package / name, ignore=shutil.ignore_patterns('__pycache__'))
        script = package / 'ratio_service_smoke.py'
        shutil.copyfile(__file__, script)
        root = base / 'fixture'
        files = {
            'proc/sys/kernel/random/boot_id': 'fixture-boot\n',
            'proc/net/route': 'Iface Destination Gateway Flags RefCnt Use Metric Mask\neth0 00000000 0100000A 0003 0 0 100 00000000\n',
            'proc/net/dev': 'eth0: 6000000000 0 0 0 0 0 0 0 5000000000 0 0 0 0 0 0 0\n',
        }
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        quota = root / 'var/lib/traffic-scheduler'
        meter = root / 'var/lib/vps-toolkit/traffic'
        for folder in (quota, meter):
            folder.mkdir(parents=True)
            os.chown(folder, account.pw_uid, account.pw_gid)
            os.chmod(folder, 0o700)
        dropin = (source / 'services/traffic-scheduler-toolkit.conf').read_text()
        added = [part for line in dropin.splitlines() if line.startswith('ReadWritePaths=')
                 for part in line.partition('=')[2].split()]
        assert added == ['/var/lib/vps-toolkit/traffic']
        for expected, paths in (('blocked', [quota]), ('writable', [quota] + [root / p.lstrip('/') for p in added])):
            properties = ['User=' + account.pw_name, 'Group=' + group, 'ProtectSystem=strict',
                          'ProtectHome=yes', 'PrivateTmp=yes', 'NoNewPrivileges=yes',
                          'ReadWritePaths=' + ' '.join(str(p) for p in paths)]
            command = ['systemd-run', '--quiet', '--wait', '--pipe', '--collect',
                       '--unit=vps-toolkit-ratio-' + uuid.uuid4().hex]
            for prop in properties:
                command += ['--property', prop]
            subprocess.run(command + [sys.executable, str(script), '--worker', str(root), expected],
                           check=True, timeout=60)
        state = json.loads((meter / 'machine-meter.json').read_text())
        assert (state['tx'], state['rx']) == (5_000_000_000, 6_000_000_000)


if __name__ == '__main__':
    main()
