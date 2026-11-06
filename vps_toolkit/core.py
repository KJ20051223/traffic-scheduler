import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


class Error(Exception):
    pass


def redact(text):
    text = re.sub(r'(?im)(authorization\s*:\s*)[^\r\n]+', r'\1[REDACTED]', str(text))
    text = re.sub(r'(?i)("?(?:token|secret|password|access.?key)"?\s*[:=]\s*"?)[^"\s,;]+', r'\1[REDACTED]', text)
    return re.sub(r'(https?://)[^/\s@]+:[^/\s@]+@', r'\1[REDACTED]@', text)


def run(argv, timeout=30, check=False, interactive=False, cwd=None):
    try:
        result = subprocess.run([str(a) for a in argv], capture_output=not interactive, text=True,
                                errors='replace', timeout=timeout, stdin=None if interactive else subprocess.DEVNULL, cwd=cwd)
        result.stdout, result.stderr = result.stdout or '', result.stderr or ''
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = subprocess.CompletedProcess(argv, 127, '', redact(exc))
    if check and result.returncode:
        raise Error(redact('命令失败: {}\n{}'.format(argv[0], (result.stderr or result.stdout)[-4000:])))
    return result


def atomic(path, data, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data if isinstance(data, bytes) else data.encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode if mode is not None else (path.stat().st_mode & 0o777 if path.exists() else 0o600))
        if os.name == 'posix' and os.geteuid() == 0:
            owner = path.stat() if path.exists() and not path.is_symlink() else path.parent.stat()
            os.chown(temporary, owner.st_uid, owner.st_gid)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise Error('配置或状态损坏，拒绝清零: {} ({})'.format(path, exc))


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


class Context:
    def __init__(self, root='/', runner=run):
        self.root = Path(root).resolve()
        self.run = runner

    def path(self, absolute):
        name = Path(str(absolute).lstrip('/'))
        if '..' in name.parts or name.is_absolute():
            raise Error('不允许越界路径')
        result = self.root / name
        # Do not follow symlinks outside the selected filesystem root.
        if self.root != Path('/').resolve() and self.root not in result.resolve().parents and result.resolve() != self.root:
            raise Error('路径超出所选根目录')
        return result

    def execute(self, argv, **kwargs):
        if self.root != Path('/').resolve() and self.run is run:
            raise Error('离线根目录模式禁止执行主机命令')
        return self.run(argv, **kwargs)

    def require_root(self):
        if os.name != 'posix' or os.geteuid() != 0:
            raise Error('此操作需要 Linux root 权限')

    @property
    def config_path(self):
        return self.path('/etc/vps-toolkit/config.json')

    @property
    def state(self):
        return self.path('/var/lib/vps-toolkit')

    def event(self, operation, **fields):
        path = self.path('/var/log/vps-toolkit/events.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        # Bounded own log; never delete operating-system or vendor audit logs.
        if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
            os.replace(str(path), str(path.with_suffix('.previous.jsonl')))
        with path.open('a', encoding='utf-8') as handle:
            handle.write(redact(json.dumps(dict(at=dt.datetime.now(dt.timezone.utc).isoformat(),
                                               operation=operation, **fields), ensure_ascii=False)) + '\n')
        os.chmod(str(path), 0o600)


@contextlib.contextmanager
def lock(directory):
    import fcntl
    Path(directory).mkdir(parents=True, exist_ok=True)
    with (Path(directory) / 'run.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error('已有操作正在运行，请稍后重试')
        yield


def confirm(component, supplied=None, prompt=input):
    if supplied == component:
        return
    if supplied is not None or prompt('确认执行请输入 {}: '.format(component)).strip() != component:
        raise Error('操作已取消')


def defaults():
    return {'version': 1, 'cloud_override': None, 'trusted_sources': [],
            'forwarder': {'config': '/etc/realm/config.json', 'binary': '/usr/local/bin/realm', 'service': 'realm'},
            'traffic': {'enabled': False, 'timezone': 'system', 'daily_bytes': 20_000_000_000,
                        'monthly_bytes': 600_000_000_000, 'task_bytes': 100_000_000,
                        'mbps': 5.0, 'max_runtime': 300, 'max_concurrency': 4, 'download_mode': 'file',
                        'link_mbps': None, 'reserve_mbps': 0, 'authorized_urls': [],
                        'region': 'cn', 'state_dir': '/var/lib/traffic-scheduler',
                        'times': [], 'window': ['00:00', '23:59'], 'paused': False,
                        'hourly_weights': [1] * 24, 'strategy': 'quota', 'ratio': '1:2',
                        'ratio_interface': 'auto', 'ratio_tolerance_pct': 5.0, 'ratio_schedule': 'follow'}}


def configuration(ctx):
    config = read_json(ctx.config_path)
    return config if config is not None else defaults()


def save_config(ctx, config):
    from .traffic import validate as validate_traffic
    validate_traffic(config['traffic'])
    if ctx.config_path.exists():
        backup_file(ctx, ctx.config_path)
    write_json(ctx.config_path, config)
    if os.name == 'posix' and os.geteuid() == 0:
        import grp
        try:
            group = grp.getgrnam('traffic-scheduler').gr_gid
        except KeyError:
            return
        os.chown(str(ctx.config_path), 0, group)
        os.chmod(str(ctx.config_path), 0o640)


def backup_file(ctx, path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise Error('只能备份普通文件: ' + str(path))
    folder = ctx.state / 'backups' / ('files-' + str(time.time_ns()))
    folder.mkdir(parents=True, mode=0o700)
    target = folder / path.name
    shutil.copy2(str(path), str(target))
    os.chmod(str(target), 0o600)
    return target
