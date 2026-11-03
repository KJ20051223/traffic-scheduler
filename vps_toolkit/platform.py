import os
import platform as host_platform
import re
import shutil
from .core import Error


def os_release(ctx):
    path = ctx.path('/etc/os-release')
    result = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
            if re.match(r'^[A-Z_]+=', line):
                key, value = line.split('=', 1)
                result[key] = value.strip().strip('"\'')
    return result


def detect(ctx):
    release = os_release(ctx)
    identifier = release.get('ID', '')
    like = release.get('ID_LIKE', '').split()
    families = [('apt-get', ['debian', 'ubuntu']), ('apk', ['alpine']),
                ('dnf', ['rhel', 'fedora', 'centos', 'rocky', 'almalinux', 'ol', 'alinux',
                         'tencentos', 'tencentosserver', 'tlinux', 'anolis', 'openEuler', 'openeuler', 'amzn']),
                ('zypper', ['opensuse-leap', 'opensuse-tumbleweed']), ('pacman', ['arch', 'manjaro'])]
    package = next((manager for manager, ids in families if any(x in ids for x in [identifier] + like)), None)
    if package == 'dnf' and not shutil.which('dnf') and shutil.which('yum'):
        package = 'yum'
    if ctx.path('/run/systemd/system').is_dir():
        init = 'systemd'
    elif ctx.path('/run/openrc').exists() or ctx.path('/sbin/openrc-run').exists():
        init = 'openrc'
    else:
        init = 'unknown'
    machine = host_platform.machine().lower()
    arch = {'amd64': 'x86_64', 'x86_64': 'x86_64', 'aarch64': 'aarch64', 'arm64': 'aarch64'}.get(machine)
    libc, version = host_platform.libc_ver()
    musl = list(ctx.path('/lib').glob('ld-musl-*.so.1')) if ctx.path('/lib').exists() else []
    if musl:
        libc = 'musl'
    warnings = []
    if identifier == 'debian' and release.get('VERSION_ID') == '11':
        warnings.append('Debian 11 已结束标准 LTS；软件源可用性需自行确认，不作安全维护保证。')
    if init == 'unknown':
        warnings.append('未检测到运行中的 systemd/OpenRC，仅可执行离线检查；不能启用服务。')
    return dict(os=release, package_manager=package, init=init, architecture=arch,
                libc=libc, libc_version=version, warnings=warnings)


def service(ctx, name, action, check=False):
    if not re.fullmatch(r'[a-zA-Z0-9_.@-]+', name):
        raise Error('服务名称无效')
    init = detect(ctx)['init']
    if init == 'systemd':
        argv = ['systemctl', action, name]
        if action == 'logs':
            argv = ['journalctl', '-u', name, '-n', '80', '--no-pager']
    elif init == 'openrc':
        if action in ('enable', 'disable', 'is-enabled'):
            argv = ['rc-update', {'enable': 'add', 'disable': 'del', 'is-enabled': 'show'}[action]]
            if action != 'is-enabled':
                argv += [name, 'default']
        elif action == 'logs':
            argv = ['tail', '-n', '80', str(ctx.path('/var/log/vps-toolkit/' + name + '.log'))]
        else:
            argv = ['rc-service', name, 'status' if action == 'is-active' else action]
    else:
        raise Error('没有可用的 systemd/OpenRC 服务管理器')
    result = ctx.execute(argv, timeout=35, check=check)
    if init == 'openrc' and action == 'is-enabled':
        result.returncode = 0 if any(line.split('|')[0].strip() == name for line in result.stdout.splitlines()) else 1
    return result
