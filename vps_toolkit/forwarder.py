import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import struct
import tarfile
import tempfile
from .core import Error, atomic, backup_file, configuration, read_json, write_json
from .network import fetch
from .platform import detect, service


def address(value, listen=False):
    match = re.fullmatch(r'(\[[0-9a-fA-F:%.]+\]|[A-Za-z0-9_.-]+):(\d+)', value or '')
    if not match or not 1 <= int(match.group(2)) <= 65535:
        raise Error('地址须为 IPv4:端口、[IPv6]:端口或域名:端口')
    host = match.group(1).strip('[]')
    if listen:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise Error('监听地址必须是本地 IPv4/IPv6 地址')
    return value


def validate(config):
    if not isinstance(config, dict) or not isinstance(config.get('endpoints'), list):
        raise Error('Realm 配置必须包含 endpoints 列表')
    seen = set()
    for endpoint in config['endpoints']:
        listen = address(endpoint.get('listen'), True)
        address(endpoint.get('remote'))
        if listen in seen:
            raise Error('重复的监听地址；多后端请使用 extra_remotes/balance')
        seen.add(listen)
        for remote in endpoint.get('extra_remotes', []):
            address(remote)
        network = endpoint.get('network', {})
        if not isinstance(network, dict):
            raise Error('network 必须是字典')
    return config


def load(ctx):
    settings = configuration(ctx)['forwarder']
    if any(not re.fullmatch(r'/[A-Za-z0-9_./-]+', settings[k]) for k in ('config', 'binary')):
        raise Error('Realm 路径须为不含空格或 Shell 元字符的绝对路径')
    path = ctx.path(settings['config'])
    if not path.exists():
        return settings, path, {'network': {'no_tcp': False, 'use_udp': True}, 'endpoints': []}
    if path.suffix == '.toml':
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                raise Error('编辑 TOML 配置需要 Python 3.11+ 或 tomli；原文件未修改，可直接导出备份')
        data = tomllib.loads(path.read_text(encoding='utf-8'))
    else:
        data = read_json(path)
    return settings, path, validate(data)


def save(ctx, data, apply=False):
    settings, path, previous = load(ctx)
    validate(data)
    if path.suffix == '.toml':
        raise Error('TOML 配置可导入/导出，但不自动重写；先显式导入到新的 JSON 路径，以保留注释及扩展字段')
    old_bytes = path.read_bytes() if path.exists() else None
    if old_bytes is not None:
        backup_file(ctx, path)
    write_json(path, data)
    if apply:
        try:
            service(ctx, settings['service'], 'restart', check=True)
            if service(ctx, settings['service'], 'is-active').returncode:
                raise Error('Realm 启动后状态异常')
        except Exception:
            if old_bytes is not None:
                atomic(path, old_bytes)
                service(ctx, settings['service'], 'restart')
            else:
                path.unlink(missing_ok=True)
                service(ctx, settings['service'], 'stop')
            logs = service(ctx, settings['service'], 'logs')
            raise Error('Realm 启动失败，已回滚本次配置；日志: ' + (logs.stdout + logs.stderr)[-3000:])
    ctx.event('pf.config', rules=len(data['endpoints']), applied=apply)
    return data


def rule(ctx, action, index=None, listen=None, remote=None, protocol='both', apply=False):
    settings, path, data = load(ctx)
    if action == 'list':
        return data
    if action in ('edit', 'delete') and (index is None or not 0 <= index < len(data['endpoints'])):
        raise Error('规则序号不存在（从 0 开始）')
    if action == 'delete':
        del data['endpoints'][index]
    else:
        endpoint = dict(data['endpoints'][index]) if action == 'edit' else {}
        if listen is not None:
            endpoint['listen'] = address(listen, True)
        if remote is not None:
            endpoint['remote'] = address(remote)
        if protocol not in ('tcp', 'udp', 'both'):
            raise Error('协议必须为 tcp、udp 或 both')
        endpoint['network'] = dict(endpoint.get('network', {}), no_tcp=protocol == 'udp', use_udp=protocol != 'tcp')
        if action == 'edit':
            data['endpoints'][index] = endpoint
        else:
            data['endpoints'].append(endpoint)
    return save(ctx, data, apply)


def inspect_elf(path, architecture):
    content = Path(path).read_bytes()
    if len(content) < 64 or content[:5] != b'\x7fELF\x02' or content[5] != 1:
        raise Error('下载内容不是 64 位小端 ELF 可执行文件')
    machine = struct.unpack_from('<H', content, 18)[0]
    if machine != {'x86_64': 62, 'aarch64': 183}.get(architecture):
        raise Error('Realm 架构与 CPU 不匹配')
    phoff = struct.unpack_from('<Q', content, 32)[0]
    entsize, count = struct.unpack_from('<HH', content, 54)
    interpreter = None
    for number in range(count):
        start = phoff + number * entsize
        if entsize < 56 or start + 56 > len(content):
            raise Error('ELF 程序头损坏')
        if struct.unpack_from('<I', content, start)[0] == 3:
            offset, size = struct.unpack_from('<Q', content, start + 8)[0], struct.unpack_from('<Q', content, start + 32)[0]
            interpreter = content[offset:offset + size].rstrip(b'\0').decode('ascii')
    versions = sorted({tuple(map(int, v.split(b'.'))) for v in re.findall(rb'GLIBC_(\d+\.\d+)', content)})
    return {'machine': machine, 'interpreter': interpreter, 'required_glibc': versions[-1] if versions else None}


def install(ctx, archive=None, restart=False):
    ctx.require_root()
    info = detect(ctx)
    if not info['architecture']:
        raise Error('只支持 x86_64 和 ARM64')
    manifest = read_json(Path(__file__).resolve().parents[1] / 'realm-assets.json')
    name = 'realm-{}-unknown-linux-musl.tar.gz'.format(info['architecture'])
    asset = manifest['assets'][name]
    settings = configuration(ctx)['forwarder']
    binary = ctx.path(settings['binary'])
    binary.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.realm-stage-', dir=str(binary.parent)) as work:
        package = Path(work) / 'realm.tar.gz'
        mirrors = [base.rstrip('/') + '/' + name for base in configuration(ctx).get('trusted_sources', [])]
        fetch(asset['url'], package, asset['sha256'], mirrors=mirrors, local=archive, kind='gzip')
        with tarfile.open(str(package), 'r:gz') as bundle:
            entries = bundle.getmembers()
            candidates = [m for m in entries if Path(m.name).name == 'realm' and m.isfile()]
            if len(candidates) != 1 or candidates[0].size > 100 * 1024 * 1024:
                raise Error('Realm 压缩包结构无效')
            candidate = Path(work) / 'realm'
            with bundle.extractfile(candidates[0]) as incoming:
                atomic(candidate, incoming.read(), 0o755)
        elf = inspect_elf(candidate, info['architecture'])
        if elf['interpreter'] and not ctx.path(elf['interpreter']).exists():
            raise Error('Realm 缺少动态加载器，拒绝升级 libc: ' + elf['interpreter'])
        if elf['required_glibc']:
            current = tuple(int(x) for x in (info['libc_version'] or '0').split('.'))
            if info['libc'] != 'glibc' or current < tuple(elf['required_glibc']):
                raise Error('GLIBC 版本不匹配，原二进制保持不变')
        ctx.execute([str(candidate), '--version'], timeout=10, check=True)
        old = binary.read_bytes() if binary.exists() else None
        if old is not None:
            backup_file(ctx, binary)
        atomic(binary, candidate.read_bytes(), 0o755)
        try:
            register(ctx)
            if restart:
                service(ctx, settings['service'], 'restart', check=True)
                if service(ctx, settings['service'], 'is-active').returncode:
                    raise Error('Realm 未进入 active 状态')
        except Exception:
            if old is not None:
                atomic(binary, old, 0o755)
                if restart:
                    service(ctx, settings['service'], 'restart')
            elif binary.exists():
                binary.unlink()
            raise
    ctx.event('pf.install', version=manifest['version'], restarted=restart)
    return dict(version=manifest['version'], elf=elf, restarted=restart,
                message='二进制已验证安装；现有运行进程无需重启，只有显式 --restart 才应用到运行服务')


def register(ctx):
    settings, config_path, data = load(ctx)
    if not config_path.exists():
        write_json(config_path, data)
    init = detect(ctx)['init']
    if init == 'systemd':
        unit = ctx.path('/etc/systemd/system/' + settings['service'] + '.service')
        if unit.exists():
            return
        text = '''# Managed by vps-toolkit
[Unit]
Description=Realm TCP and UDP forwarder
After=network-online.target
[Service]
ExecStart={binary} -c {config}
Restart=no
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
'''.format(binary=settings['binary'], config=settings['config'])
        atomic(unit, text, 0o644)
        ctx.execute(['systemctl', 'daemon-reload'], check=True)
    elif init == 'openrc':
        unit = ctx.path('/etc/init.d/' + settings['service'])
        if unit.exists():
            return
        atomic(unit, '''#!/sbin/openrc-run
# Managed by vps-toolkit
name="Realm"
command="{binary}"
command_args="-c {config}"
command_background=true
pidfile="/run/realm.pid"
depend() {{ need net; }}
'''.format(binary=settings['binary'], config=settings['config']), 0o755)
    else:
        raise Error('Realm 二进制已验证，但当前环境没有可用的服务管理器')


def diagnostics(ctx):
    settings = configuration(ctx)['forwarder']
    result = {'binary': settings['binary'], 'config': settings['config']}
    path = ctx.path(settings['binary'])
    if path.exists():
        try:
            result['elf'] = inspect_elf(path, detect(ctx)['architecture'])
            response = ctx.execute([str(path), '--version'], timeout=5)
            result['version'] = response.stdout or response.stderr
        except Error as exc:
            result['error'] = str(exc)
    try:
        result['rules'] = load(ctx)[2]
    except (Error, ValueError) as exc:
        result['config_error'] = str(exc)
    result['service'] = service(ctx, settings['service'], 'status').stdout
    result['logs'] = service(ctx, settings['service'], 'logs').stdout[-4000:]
    result['listeners'] = ctx.execute(['ss', '-lntup']).stdout
    return result
