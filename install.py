"""Transactional installer. Does not stop, start or enable existing services."""
import argparse
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sys
import tempfile
from vps_toolkit.core import Context, Error, atomic, configuration, digest, save_config, write_json
from vps_toolkit.platform import detect
from vps_toolkit.traffic import import_legacy
from vps_toolkit import __version__


SOURCE = Path(__file__).resolve().parent


def install(ctx, python=sys.executable, no_wizard=False):
    ctx.require_root()
    manifest = json.loads((SOURCE / 'MANIFEST.json').read_text())
    for name, expected in manifest.items():
        path = SOURCE / name
        if '..' in Path(name).parts or not path.is_file() or digest(path) != expected:
            raise Error('安装包校验失败: ' + name)
    target = ctx.path('/opt/vps-toolkit')
    target.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(str(target)).free < 64 * 1024 * 1024:
        raise Error('安装至少需要 64 MB 可用空间')
    current = target / 'current'
    if current.exists() and not current.is_symlink():
        raise Error('/opt/vps-toolkit/current 不是本工具符号链接，拒绝覆盖')
    release_hash = hashlib.sha256((SOURCE / 'MANIFEST.json').read_bytes()).hexdigest()[:16]
    release = target / 'releases' / (__version__ + '-' + release_hash)
    old_target = os.readlink(str(current)) if current.is_symlink() else None
    owned = ctx.path('/usr/local/bin/vps-toolkit')
    if owned.exists() and 'VPS_TOOLKIT_MANAGED' not in owned.read_text(errors='replace')[:300]:
        raise Error('系统存在其他同名 vps-toolkit，拒绝覆盖')
    config = import_legacy(ctx)
    previous = {}
    def replace(path, text, mode):
        previous[path] = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
        atomic(path, text, mode)
    try:
        if not release.exists():
            release.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix='.stage-', dir=str(release.parent)))
            for name in manifest:
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(SOURCE / name), str(destination))
                os.chmod(str(destination), 0o644)
            shutil.copyfile(str(SOURCE / 'MANIFEST.json'), str(stage / 'MANIFEST.json'))
            for folder in [stage] + [p for p in stage.rglob('*') if p.is_dir()]:
                os.chmod(str(folder), 0o755)
            os.replace(str(stage), str(release))
        account = pwd.getpwnam('traffic-scheduler')
        if account.pw_uid == 0 or grp.getgrnam('traffic-scheduler').gr_gid != account.pw_gid:
            raise Error('traffic-scheduler 账户身份异常，拒绝以 root 进行下载')
        for name in ('/var/lib/traffic-scheduler', '/var/lib/vps-toolkit/traffic', '/var/log/vps-toolkit'):
            folder = ctx.path(name)
            folder.mkdir(parents=True, exist_ok=True)
            if name != '/var/log/vps-toolkit':
                os.chown(str(folder), account.pw_uid, account.pw_gid)
                os.chmod(str(folder), 0o700)
        ctx.state.mkdir(parents=True, exist_ok=True)
        os.chown(str(ctx.state), 0, account.pw_gid)
        os.chmod(str(ctx.state), 0o750)
        config_folder = ctx.config_path.parent
        config_folder.mkdir(parents=True, exist_ok=True)
        os.chown(str(config_folder), 0, account.pw_gid)
        os.chmod(str(config_folder), 0o750)
        previous[ctx.config_path] = (ctx.config_path.read_bytes(), ctx.config_path.stat().st_mode & 0o777) if ctx.config_path.exists() else None
        save_config(ctx, config)
        launcher = '#!/bin/sh\n# VPS_TOOLKIT_MANAGED\nexec "{}" /opt/vps-toolkit/current/run.py "$@"\n'.format(python)
        replace(owned, launcher, 0o755)
        pf = ctx.path('/usr/local/bin/pf')
        if not pf.exists():
            replace(pf, launcher.replace('run.py "$@"', 'run.py pf "$@"'), 0o755)
        legacy = ctx.path('/usr/local/bin/traffic-scheduler')
        if not legacy.exists() or 'Traffic Scheduler:' in legacy.read_text(errors='replace')[:300]:
            text = (SOURCE / 'compat/traffic_scheduler.py').read_text(encoding='utf-8')
            replace(legacy, '#!' + python + '\n' + text.split('\n', 1)[1], 0o755)
        old_config = ctx.path('/etc/traffic-scheduler/config.json')
        if not old_config.exists():
            from compat.traffic_scheduler import default_config
            replace(old_config, json.dumps(default_config(), indent=2), 0o640)
            os.chown(str(old_config), 0, account.pw_gid)
            os.chown(str(old_config.parent), 0, account.pw_gid)
            os.chmod(str(old_config.parent), 0o750)
        info = detect(ctx)
        if info['init'] == 'systemd':
            for name in ('vps-toolkit-traffic.service', 'vps-toolkit-traffic.timer'):
                destination = ctx.path('/etc/systemd/system/' + name)
                if destination.exists() and 'Managed by vps-toolkit' not in destination.read_text():
                    raise Error('存在其他同名服务，拒绝覆盖')
                replace(destination, (SOURCE / 'services' / name).read_text().replace('/usr/bin/python3', python), 0o644)
            old_service = ctx.path('/etc/systemd/system/traffic-scheduler.service')
            if old_service.is_file() and '/usr/local/bin/traffic-scheduler' in old_service.read_text():
                dropin = ctx.path('/etc/systemd/system/traffic-scheduler.service.d/50-vps-toolkit.conf')
                if dropin.is_symlink() or dropin.exists() and 'Managed by vps-toolkit' not in dropin.read_text():
                    raise Error('已有其他同名旧任务兼容配置，拒绝覆盖')
                replace(dropin, (SOURCE / 'services/traffic-scheduler-toolkit.conf').read_text(), 0o644)
        elif info['init'] == 'openrc':
            destination = ctx.path('/etc/init.d/vps-toolkit-traffic')
            if destination.exists() and 'Managed by vps-toolkit' not in destination.read_text():
                raise Error('存在其他同名 OpenRC 服务')
            replace(destination, (SOURCE / 'services/vps-toolkit-traffic.openrc').read_text().replace('/usr/bin/python3', python), 0o755)
        pending = target / '.next'
        pending.unlink(missing_ok=True)
        pending.symlink_to(release.relative_to(target), target_is_directory=True)
        os.replace(str(pending), str(current))
        if info['init'] == 'systemd':
            ctx.execute(['systemctl', 'daemon-reload'], check=True)
        write_json(ctx.state / 'installation.json', {'version': __version__, 'release': str(release),
                    'previous_release': old_target, 'legacy_tasks_preserved': True})
    except Exception:
        for path, value in previous.items():
            if value is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path, value[0], value[1])
        if old_target:
            pending = target / '.rollback'
            pending.unlink(missing_ok=True)
            pending.symlink_to(old_target, target_is_directory=True)
            os.replace(str(pending), str(current))
        elif current.is_symlink():
            current.unlink()
        raise
    print('安装完成：vps-toolkit；已有 PF/Realm 和下载任务状态保留，新定时任务默认关闭。')
    if not no_wizard and sys.stdin.isatty():
        from vps_toolkit.cli import main
        return main(['wizard'], ctx)
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-wizard', action='store_true')
    args = parser.parse_args()
    try:
        raise SystemExit(install(Context(), no_wizard=args.no_wizard))
    except (Error, OSError, ValueError) as error:
        print('安装失败，已回滚自身文件改动: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
