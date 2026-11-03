import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import tarfile
from .core import Error, atomic, confirm, read_json


PREFIXES = ('etc/vps-toolkit/', 'etc/traffic-scheduler/', 'etc/realm/', 'var/lib/traffic-scheduler/')


def create(ctx, output=None):
    target = Path(output) if output else ctx.state / 'backups' / ('backup-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.tar.gz')
    target.parent.mkdir(parents=True, exist_ok=True)
    files = []
    for prefix in PREFIXES:
        folder = ctx.path('/' + prefix)
        if folder.exists():
            files += [p for p in folder.rglob('*') if p.is_file() and not p.is_symlink() and p.name != 'run.lock']
    manifest = {}
    with tarfile.open(str(target), 'w:gz') as archive:
        for path in files:
            if path.stat().st_size > 16 * 1024 * 1024:
                raise Error('配置备份中存在异常大文件')
            name = path.relative_to(ctx.root).as_posix()
            data = path.read_bytes()
            manifest[name] = hashlib.sha256(data).hexdigest()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))
        data = json.dumps(manifest, sort_keys=True).encode()
        info = tarfile.TarInfo('manifest.json')
        info.size, info.mode = len(data), 0o600
        archive.addfile(info, io.BytesIO(data))
    os.chmod(str(target), 0o600)
    return {'archive': str(target), 'files': len(manifest)}


def restore(ctx, archive, dry_run=True, confirmation=None):
    prepared = {}
    with tarfile.open(str(archive), 'r:*') as bundle:
        entries = bundle.getmembers()
        if sum(e.size for e in entries) > 200 * 1024 * 1024:
            raise Error('备份超出允许大小')
        manifest_member = next((m for m in entries if m.name == 'manifest.json' and m.isfile()), None)
        if manifest_member is None:
            raise Error('备份缺少文件校验清单')
        manifest = json.load(bundle.extractfile(manifest_member))
        for entry in entries:
            if entry.name == 'manifest.json':
                continue
            path = PurePosixPath(entry.name)
            if (not entry.isfile() or path.is_absolute() or '..' in path.parts
                    or not entry.name.startswith(PREFIXES) or entry.name in prepared):
                raise Error('备份包含不允许的路径或文件类型')
            data = bundle.extractfile(entry).read()
            if hashlib.sha256(data).hexdigest() != manifest.get(entry.name):
                raise Error('备份文件校验失败: ' + entry.name)
            destination = ctx.path('/' + entry.name)
            if destination.is_symlink():
                raise Error('不覆盖符号链接配置')
            if destination.name.startswith('quota-') and destination.exists():
                incoming = json.loads(data)
                current = read_json(destination)
                # Restoring configuration must never roll back traffic already spent.
                for key in ('bytes', 'confirmed'):
                    incoming['download'][key] = max(incoming['download'][key], current['download'][key])
                incoming['download']['bytes'] = max(incoming['download']['bytes'], current['download']['bytes'] + current['download'].get('reserved', 0))
                incoming['download']['reserved'] = 0
                data = (json.dumps(incoming, ensure_ascii=False, indent=2) + '\n').encode()
            prepared[entry.name] = data
        if set(prepared) != set(manifest):
            raise Error('备份清单和文件集合不匹配')
    if dry_run:
        return {'files': sorted(prepared), 'applied': False}
    ctx.require_root()
    confirm('restore', confirmation)
    previous = {name: ctx.path('/' + name).read_bytes() if ctx.path('/' + name).exists() else None for name in prepared}
    try:
        for name, data in prepared.items():
            atomic(ctx.path('/' + name), data)
    except Exception:
        for name, data in previous.items():
            path = ctx.path('/' + name)
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path, data)
        raise
    ctx.event('backup.restore', files=len(prepared))
    return {'files': sorted(prepared), 'applied': True, 'services_restarted': False,
            'note': '已恢复配置且保留较大的计量值；服务状态未改变，需要时显式重启对应模块。'}
