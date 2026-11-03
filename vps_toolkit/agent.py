import os
from pathlib import Path
import re
import stat
import sys
from .core import Error, backup_file, configuration, confirm, digest, redact, write_json
from .health import identify_cloud
from .platform import detect


ALI_SECURITY = 'https://www.alibabacloud.com/help/zh/security-center/uninstall-the-security-center-agent'
ALI_ASSIST = 'https://help.aliyun.com/zh/ecs/user-guide/install-cloud-assistant-agent'
ALI_MONITOR = 'https://help.aliyun.com/en/cms/cloudmonitor-1-0/user-guide/install-and-uninstall-the-cloudmonitor-agent-for-cpp'
TAT_DOC = 'https://cloud.tencent.com/document/product/1340/46050'
TC_MONITOR = 'https://intl.cloud.tencent.com/zh/document/product/248/39810'
TC_SECURITY = 'https://cloud.tencent.com/document/product/664/137804'


CATALOG = {
    'aliyun-assist': dict(provider='aliyun', names=['aliyun-service', 'AssistDaemon', 'aliyun-assist'],
                          paths=['/usr/local/share/aliyun-assist', '/usr/local/aliyun-assist'], doc=ALI_ASSIST,
                          uninstall=[]),
    'aegis': dict(provider='aliyun', names=['AliYunDun', 'AliYunDunMonitor', 'AliYunDunUpdate', 'AliSecCheck', 'aegis'],
                  paths=['/usr/local/aegis', '/opt/aegis'], doc=ALI_SECURITY, protected=True, uninstall=[]),
    'cloudmonitor': dict(provider='aliyun', names=['argusagent', 'cloudmonitor', 'CmsGoAgent'],
                         paths=['/usr/local/cloudmonitor', '/opt/cloudmonitor'], doc=ALI_MONITOR,
                         uninstall=[('/usr/local/cloudmonitor/cloudmonitorCtl.sh', ['uninstall']),
                                    ('/usr/local/cloudmonitor/wrapper/bin/cloudmonitor.sh', ['remove'])]),
    'tat': dict(provider='tencent', names=['tat_agent', 'tencentcloud-tat-agent', 'tat-agent'],
                paths=['/usr/local/qcloud/tat_agent'], doc=TAT_DOC,
                uninstall=[('/usr/local/qcloud/tat_agent/uninstall.sh', [])]),
    'barad': dict(provider='tencent', names=['barad_agent', 'BaradAgent', 'barad'],
                  paths=['/usr/local/qcloud/monitor/barad'], doc=TC_MONITOR,
                  uninstall=[('/usr/local/qcloud/monitor/barad/admin/uninstall.sh', [])]),
    'sgagent': dict(provider='tencent', names=['sgagent', 'Sgagent', 'stargate'],
                    paths=['/usr/local/qcloud/stargate'], doc=TC_MONITOR,
                    uninstall=[('/usr/local/qcloud/stargate/admin/uninstall.sh', [])]),
    'yunjing': dict(provider='tencent', names=['YDService', 'YDLive', 'YDPython', 'YDGuard', 'YunJing'],
                    paths=['/usr/local/qcloud/YunJing'], doc=TC_SECURITY, protected=True, uninstall=[]),
    'cloud-init': dict(provider=None, names=['cloud-init', 'cloud-final', 'cloud-config', 'cloud-id'],
                       paths=['/etc/cloud', '/var/lib/cloud'],
                       doc='https://cloudinit.readthedocs.io/en/latest/howto/disable_cloud_init.html', uninstall=[])}


def snapshot(ctx):
    processes, unreadable = [], []
    proc = ctx.path('/proc')
    if not proc.exists() or not os.access(str(proc), os.R_OK):
        unreadable.append('无法读取 /proc，进程缺失不能视为已卸载')
    for entry in list(proc.glob('[0-9]*/comm'))[:50000]:
        try:
            processes.append(entry.read_text(errors='replace').strip())
        except OSError:
            unreadable.append('部分进程信息无读取权限')
    init = detect(ctx)['init']
    units, active = '', ''
    if init == 'systemd':
        units = ctx.execute(['systemctl', 'list-unit-files', '--no-legend', '--no-pager'], timeout=10)
        active = ctx.execute(['systemctl', 'list-units', '--all', '--no-legend', '--no-pager'], timeout=10)
        if units.returncode or active.returncode:
            unreadable.append('部分 systemd 状态无法读取')
        units, active = units.stdout, active.stdout
    elif init == 'openrc':
        active = ctx.execute(['rc-status', '--all'], timeout=10).stdout
        units = '\n'.join(p.name for p in ctx.path('/etc/init.d').glob('*'))
    package = detect(ctx)['package_manager']
    command = {'apt-get': ['dpkg-query', '-W', '-f=${binary:Package}\t${db:Status-Status}\n'],
               'apk': ['apk', 'info'], 'dnf': ['rpm', '-qa', '--qf', '%{NAME}\n'],
               'yum': ['rpm', '-qa', '--qf', '%{NAME}\n']}.get(package)
    packages = ctx.execute(command, timeout=15) if command else None
    if packages is None or packages.returncode:
        unreadable.append('软件包列表不可确认')
    startup = []
    candidates = [ctx.path(p) for p in ['/etc/crontab', '/etc/rc.local', '/var/spool/cron/root',
                                         '/var/spool/cron/crontabs/root', '/var/log/cloud-init.log',
                                         '/var/log/cloud-init-output.log']]
    for folder in ['/etc/cron.d', '/etc/systemd/system', '/etc/init.d', '/var/lib/cloud/scripts/per-boot']:
        candidates += list(ctx.path(folder).glob('*'))[:2000]
    for file in candidates:
        if file.is_file() and not file.is_symlink():
            try:
                text = file.read_text(errors='replace')[:2_000_000]
                startup.append((str(file.relative_to(ctx.root)), text))
            except OSError:
                unreadable.append('部分启动项或安装记录无法读取')
    return dict(processes=processes, units=units, active=active,
                packages=packages.stdout if packages else '', startup=startup,
                limitations=sorted(set(unreadable)))


def audit(ctx):
    facts = snapshot(ctx)
    results = []
    for identifier, item in CATALOG.items():
        pattern = re.compile('|'.join(re.escape(n) for n in item['names']), re.I)
        paths = [p for p in item['paths'] if ctx.path(p).exists()]
        processes = sorted({n for n in facts['processes'] if pattern.search(n)})
        units = sorted({line.split()[0] for line in facts['units'].splitlines() if pattern.search(line) and line.split()})
        running_units = [line.split()[0] for line in facts['active'].splitlines()
                         if pattern.search(line) and re.search(r'\b(running|started|active)\b', line, re.I)]
        packages = [line.split()[0] for line in facts['packages'].splitlines()
                    if pattern.search(line) and ('\t' not in line or '\tinstalled' in line)]
        startup = [path for path, text in facts['startup'] if pattern.search(text)]
        state = '运行中' if processes or running_units else '已安装/残留，未确认运行' if paths or units or packages else '未发现已知组件'
        if state == '未发现已知组件' and startup:
            state = '仅发现安装/启动记录，未确认安装'
        if state == '未发现已知组件' and facts['limitations']:
            state = '无法确定'
        results.append(dict(id=identifier, state=state, paths=paths, processes=processes,
                            services=units, packages=packages, startup_or_install_records=startup,
                            protection='需在控制台核实' if item.get('protected') else '不适用/未核实',
                            official_document=item['doc']))
    cloud = identify_cloud(ctx, configuration(ctx).get('cloud_override'))
    return {'cloud': cloud, 'components': results, 'limitations': facts['limitations'],
            'layers': {'guest_agents': '仅针对以上已知组件给出证据',
                       'initialization': 'cloud-init、启动任务和安装记录单独检查',
                       'hypervisor_monitoring': '客户机无法关闭或验证其完整状态',
                       'cloud_network_metrics': '不因客户机 Agent 卸载而消失',
                       'agentless_detection': '客户机无法完整核实'},
            'console_notice': '本地未发现已知客户端 Agent 时，无法单凭客户机检查确认控制台状态。请核对云安全中心客户端状态、最近心跳和资产同步信息。'}


def trusted_local_script(path, expected):
    if not path.is_file() or path.is_symlink():
        raise Error('官方卸载程序不存在或为符号链接；请按官方文档处理')
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise Error('官方卸载程序必须由 root 拥有且不可被其他用户写入')
    if not expected or digest(path) != expected.lower():
        raise Error('需提供通过可信渠道核实的官方卸载程序 SHA256；不会自行下载并执行未知脚本')
    if not path.read_bytes().lstrip().startswith(b'#!'):
        raise Error('卸载文件不是合法脚本')


def remove(ctx, identifier, dry_run=False, confirmation=None, console_authorized=False,
           script=None, sha256=None, network_independent=False):
    if identifier not in CATALOG:
        raise Error('未知组件')
    component = CATALOG[identifier]
    before = audit(ctx)
    observed = next(c for c in before['components'] if c['id'] == identifier)
    if observed['state'] == '未发现已知组件':
        return dict(skipped=True, reason='未发现组件', audit=observed)
    provider = before['cloud']['provider']
    if component['provider'] and provider != component['provider']:
        raise Error('平台未知或不匹配；先核实并使用 cloud select 手动选择，禁止跨平台卸载')
    plan = {'component': identifier, 'official_document': component['doc'],
            'impact': '会失去该组件提供的管理、监控或安全功能；历史数据和控制台资产可能无法恢复。',
            'console_precondition': bool(component.get('protected'))}
    if identifier == 'cloud-init':
        return remove_cloud_init(ctx, plan, dry_run, confirmation, network_independent)
    choices = component['uninstall']
    chosen = next(((ctx.path(p), args) for p, args in choices if ctx.path(p).is_file()), None)
    if script:
        chosen = (Path(script).resolve(), [])
    plan['command'] = ['bash', str(chosen[0])] + chosen[1] if chosen else None
    if dry_run:
        return plan
    ctx.require_root()
    if component.get('protected') and not console_authorized:
        raise Error('需先按官方文档在控制台完成自保护/卸载授权，再明确传 --console-authorized。' + component['doc'])
    if not chosen:
        raise Error('未找到可验证的官方卸载程序。请从官方文档取得程序，使用 --official-script 和 --sha256；不强制删除目录。' + component['doc'])
    trusted_local_script(chosen[0], sha256)
    confirm(identifier, confirmation)
    ctx.event('agent.remove.begin', component=identifier, script_sha256=sha256)
    result = ctx.execute(plan['command'], timeout=300, interactive=bool(component.get('protected') and sys.stdin.isatty()))
    after = audit(ctx)
    ctx.event('agent.remove.end', component=identifier, returncode=result.returncode)
    return {'returncode': result.returncode, 'output': redact((result.stdout + result.stderr)[-5000:]),
            'verified': next(c for c in after['components'] if c['id'] == identifier),
            'reinstall': component['doc']}


def remove_cloud_init(ctx, plan, dry_run, confirmation, independent):
    package = detect(ctx)['package_manager']
    commands = {'apt-get': (['apt-get', '-s', 'remove', 'cloud-init'], ['apt-get', 'remove', '-y', 'cloud-init']),
                'dnf': (['dnf', '--assumeno', 'remove', 'cloud-init'], ['dnf', '-y', 'remove', 'cloud-init']),
                'yum': (['yum', '--assumeno', 'remove', 'cloud-init'], ['yum', '-y', 'remove', 'cloud-init'])}
    if package not in commands:
        raise Error('此发行版仅提供 cloud-init 审计；请使用发行版官方包管理流程卸载')
    persistent = [ctx.path('/etc/network/interfaces')] + list(ctx.path('/etc/netplan').glob('*.yaml')) + list(ctx.path('/etc/NetworkManager/system-connections').glob('*')) + list(ctx.path('/etc/systemd/network').glob('*.network'))
    persistent = [p for p in persistent if p.is_file() and not p.is_symlink()]
    simulation = ctx.execute(commands[package][0], timeout=60)
    plan.update(persistent_network_files=[str(p) for p in persistent],
                simulation=redact((simulation.stdout + simulation.stderr)[-10000:]),
                simulation_returncode=simulation.returncode, requires_second_confirmation=True)
    if dry_run:
        return plan
    ctx.require_root()
    if not independent or not persistent:
        raise Error('需核实网络与 SSH 已独立持久化，并显式传 --confirm-network-independent；不会删除 50-cloud-init.yaml')
    if package == 'apt-get' and simulation.returncode:
        raise Error('软件包卸载模拟失败')
    if package in ('dnf', 'yum') and simulation.returncode and not re.search(r'(?i)(operation aborted|exiting on user command)', simulation.stdout + simulation.stderr):
        raise Error('软件包卸载模拟未正常完成，请人工核查依赖事务')
    if re.search(r'(?im)^(Remv|Removing).*\b(openssh|systemd|netplan|network-manager|iproute)', plan['simulation']):
        raise Error('模拟显示可能移除关键网络/SSH 组件，已拒绝卸载')
    confirm('cloud-init', confirmation)
    # Separate second acknowledgement is compulsory even in non-interactive mode.
    for file in persistent:
        backup_file(ctx, file)
    route = ctx.execute(['ip', 'route', 'show'], timeout=5).stdout
    result = ctx.execute(commands[package][1], timeout=300)
    after_route = ctx.execute(['ip', 'route', 'show'], timeout=5).stdout
    ctx.event('agent.cloud-init.remove', returncode=result.returncode, route_unchanged=route == after_route)
    return dict(returncode=result.returncode, route_unchanged=route == after_route,
                preserved_network_files=[str(p) for p in persistent],
                output=redact((result.stdout + result.stderr)[-4000:]), reinstall='使用发行版包管理器重新安装 cloud-init；不会自动重启网络')
