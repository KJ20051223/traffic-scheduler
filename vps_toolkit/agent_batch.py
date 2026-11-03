"""Best-effort official Agent removal with one approved, immutable execution plan."""
import datetime as dt
from pathlib import Path
import re
import shutil
import sys
from . import agent
from .core import Error, atomic, backup_file, confirm, digest, lock, redact, write_json
from .platform import detect


TAT_SOURCE = 'https://github.com/Tencent/tat-agent/blob/3c448fe41fa502d0d3294181d37018380235c6be/install/uninstall.sh'
TAT_SHA256 = 'f63d2b8fc0c79aa5c612c30bc03360c2c3a7b2f4d205eb7548acb3182a9bae58'
ORDER = ['sgagent', 'barad', 'cloudmonitor', 'aegis', 'yunjing', 'cloud-init', 'aliyun-assist', 'tat']
MONITORS = {'sgagent', 'barad', 'cloudmonitor'}


def persistent_network(ctx):
    files = [ctx.path('/etc/network/interfaces')]
    for pattern in ('/etc/network/interfaces.d/*', '/etc/netplan/*.yaml',
                    '/etc/NetworkManager/system-connections/*', '/etc/systemd/network/*.network',
                    '/etc/sysconfig/network-scripts/ifcfg-*'):
        folder, glob = pattern.rsplit('/', 1)
        files += list(ctx.path(folder).glob(glob))
    return sorted({p for p in files if p.is_file() and not p.is_symlink()}, key=str)


def script_step(path, args=(), expected=None):
    checksum = digest(path)
    if expected and checksum != expected:
        raise Error('内置官方程序摘要不匹配')
    agent.trusted_local_script(path, checksum)
    return {'kind': 'script', 'argv': ['bash', str(path)] + list(args), 'path': str(path), 'sha256': checksum}


def trusted_binary(path, expected):
    info = path.stat()
    if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022 or digest(path) != expected:
        raise Error('官方守护程序身份、权限或摘要已改变')
    if not path.read_bytes().startswith(b'\x7fELF'):
        raise Error('守护程序不是已安装 ELF 程序')


def cleanup_assist(ctx):
    observed = next(c for c in agent.audit(ctx)['components'] if c['id'] == 'aliyun-assist')
    if observed['processes']:
        raise Error('云助手进程仍存在，保留目录并报告未完成')
    # Exact directories listed in the vendor's official uninstall guide.
    for name in ('/usr/local/share/aliyun-assist', '/usr/local/share/assist-daemon'):
        target = ctx.path(name)
        if target.is_symlink() or target.resolve() != target.absolute():
            raise Error('云助手目录存在符号链接，拒绝递归清理')
        if target.is_dir():
            shutil.rmtree(str(target))


def package_spec(ctx, identifier):
    manager = detect(ctx)['package_manager']
    if manager == 'apt-get':
        package = 'aliyun-assist' if identifier == 'aliyun-assist' else 'cloud-init'
        query = ['dpkg-query', '-W', '-f=${db:Status-Status}', package]
        test, remove = ['dpkg', '--no-act', '--remove', package], ['dpkg', '--remove', package]
    elif manager in ('dnf', 'yum', 'zypper'):
        package = 'aliyun_assist' if identifier == 'aliyun-assist' else 'cloud-init'
        query = ['rpm', '-q', '--qf', '%{NAME}', package]
        test, remove = ['rpm', '-e', '--test', package], ['rpm', '-e', package]
    elif manager == 'apk' and identifier == 'cloud-init':
        package = 'cloud-init'
        query = ['apk', 'info', '-e', package]
        test, remove = ['apk', 'del', '--simulate', package], ['apk', 'del', package]
    else:
        raise Error('该系统尚无已验证的此组件包卸载流程')
    return package, query, test, remove


def package_installed(ctx, identifier):
    package, query, _, _ = package_spec(ctx, identifier)
    result = ctx.execute(query, timeout=20)
    if result.returncode not in (0, 1):
        raise Error('无法确认软件包状态：' + redact(result.stderr))
    return result.returncode == 0 and (result.stdout.strip() == 'installed' if query[0] == 'dpkg-query'
                                      else package in result.stdout.split())


def package_step(ctx, identifier):
    package, _, test, remove = package_spec(ctx, identifier)
    if not package_installed(ctx, identifier):
        raise Error('未发现该组件的软件包安装；可能仅有目录残留或使用了手动安装方式')
    result = ctx.execute(test, timeout=60)
    text = redact(result.stdout + result.stderr)[-10000:]
    if result.returncode:
        raise Error('包管理器依赖检查未通过：' + text)
    if test[0] == 'apk':
        removed = re.findall(r'\bPurging\s+(\S+)\s+\(', text)
        if not removed or any(name != package for name in removed):
            raise Error('APK 模拟未能确认仅移除此组件；保留依赖事务：' + text)
    # dpkg/rpm remove only this exact package, refuse dependency breakage, and
    # do not recursively autoremove other packages or purge network conffiles.
    return {'kind': 'package', 'argv': remove, 'identifier': identifier, 'simulation': text}


def plan(ctx, scope='all', include_cloud_init=True, console_authorized=False, network_independent=False):
    if scope not in ('all', 'monitoring'):
        raise Error('范围须为 all 或 monitoring')
    facts = agent.audit(ctx)
    entries = []
    for identifier in ORDER:
        observed = next(c for c in facts['components'] if c['id'] == identifier)
        component = agent.CATALOG[identifier]
        item = {'id': identifier, 'state': observed['state'], 'official_document': component['doc'],
                'steps': [], 'status': 'not_detected', 'reason': ''}
        concrete = any(observed.get(k) for k in ('paths', 'processes', 'services', 'packages'))
        if not concrete:
            item['reason'] = '未发现可确认的安装或运行依据；仅有历史记录不会执行卸载'
        elif (scope == 'monitoring' and identifier not in MONITORS) or (identifier == 'cloud-init' and not include_cloud_init):
            item.update(status='excluded', reason='不在本次选择范围')
        elif component['provider'] and facts['cloud']['provider'] != component['provider']:
            item.update(status='blocked', reason='平台未确认或不匹配，请先核实 cloud select')
        elif component.get('protected') and not console_authorized:
            item.update(status='blocked', reason='需先按官方文档在控制台授权卸载/关闭自保护；可能需要验证码')
        elif identifier in ('cloud-init', 'aliyun-assist', 'tat') and not network_independent:
            item.update(status='blocked', reason='需确认正通过独立 SSH/VNC 操作，且网络与 SSH 配置已持久化')
        else:
            try:
                if identifier == 'cloud-init':
                    if any(name.startswith('cloud-init') for name in observed.get('processes', [])):
                        raise Error('cloud-init 仍在执行初始化，请等待其结束后重试')
                    network = persistent_network(ctx)
                    if not network:
                        raise Error('未找到可备份的持久网络配置，请先完成网络持久化')
                    item['network_files'] = [str(p) for p in network]
                    item['network_hashes'] = {str(p): digest(p) for p in network}
                    item['fallback_disable'] = True
                    try:
                        item['steps'] = [package_step(ctx, identifier)]
                    except Error as exc:
                        item['reason'] = str(exc) + '；本次仅尝试官方禁用下次启动，不宣称已卸载'
                        item['steps'] = [{'kind': 'disable_cloud_init'}]
                elif identifier == 'aliyun-assist':
                    package = package_step(ctx, identifier)
                    daemon = ctx.path('/usr/local/share/assist-daemon/assist_daemon')
                    if daemon.is_file():
                        checksum = digest(daemon)
                        trusted_binary(daemon, checksum)
                        item['steps'] += [{'kind': 'binary', 'path': str(daemon), 'sha256': checksum,
                                           'argv': [str(daemon), option]} for option in ('--stop', '--delete')]
                    if detect(ctx)['init'] == 'systemd':
                        item['steps'].append({'kind': 'service', 'argv': ['systemctl', 'stop', 'aliyun.service']})
                    item['steps'] += [package, {'kind': 'cleanup_assist'}]
                else:
                    choices = list(component['uninstall'])
                    if identifier == 'yunjing':
                        choices = [('/usr/local/qcloud/YunJing/uninst.sh', []), ('/var/lib/qcloud/YunJing/uninst.sh', [])]
                    if identifier == 'aegis':
                        choices = [('/usr/local/aegis/uninstall.sh', [])]
                    chosen = next(((ctx.path(path), args) for path, args in choices if ctx.path(path).is_file()), None)
                    if chosen:
                        item['steps'] = [script_step(*chosen)]
                    elif identifier == 'tat':
                        asset = Path(__file__).parent / 'assets' / 'tat-uninstall.sh'
                        item['steps'] = [script_step(asset, expected=TAT_SHA256)]
                        item['source'] = TAT_SOURCE
                    else:
                        raise Error('未找到已安装的官方卸载程序；请按所列官方文档取得程序后使用单项卸载')
                item['status'] = 'ready'
            except (Error, OSError) as exc:
                item.update(status='blocked', reason=str(exc), steps=[])
        entries.append(item)
    return {'batch_plan': True, 'scope': scope, 'include_cloud_init': include_cloud_init,
            'console_authorized': console_authorized, 'network_independent': network_independent,
            'provider': facts['cloud']['provider'], 'entries': entries, 'limitations': facts['limitations'],
            'impact': '卸载所选客户机 Agent 会中断相应监控、安全防护、远程命令与免密会话；历史安全数据可能无法恢复。',
            'note': '保留 SSH、网络配置、Realm、下载任务及系统/业务日志；厂商程序可删除自身文件。卸载不会关闭宿主机/控制平面监控。'}


def disable_cloud_init(ctx):
    path = ctx.path('/etc/cloud/cloud-init.disabled')
    if path.is_symlink():
        raise Error('cloud-init 禁用标记为符号链接，拒绝覆盖')
    if path.exists():
        backup_file(ctx, path)
    atomic(path, b'', 0o600)


def execute(ctx, approved, confirmation=None):
    ctx.require_root()
    if not approved.get('batch_plan'):
        raise Error('需要先生成并确认本次执行清单')
    if any(item['status'] == 'ready' for item in approved['entries']):
        confirm('remove-all', confirmation)
    folder = ctx.state / 'agent-removal'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = {'batch_result': True, 'started': dt.datetime.now(dt.timezone.utc).isoformat(),
              'plan': approved, 'results': [], 'returncode': 0, 'report_file': str(folder / 'latest.json')}
    with lock(folder):
        if (folder / 'latest.json').exists():
            atomic(folder / 'previous.json', (folder / 'latest.json').read_bytes())
        write_json(folder / 'latest.json', report)
        ctx.event('agent.batch.begin', scope=approved['scope'])
        for item in approved['entries']:
            result = {'id': item['id'], 'status': item['status'], 'reason': item['reason'], 'output': ''}
            report['results'].append(result)
            if item['status'] != 'ready':
                if item['status'] == 'blocked':
                    report['returncode'] = 1
                write_json(folder / 'latest.json', report)
                continue
            result['status'] = 'running'
            write_json(folder / 'latest.json', report)
            try:
                # Never expand an approved batch to newly discovered components.
                current = agent.audit(ctx)
                component = agent.CATALOG[item['id']]
                if component['provider'] and component['provider'] != current['cloud']['provider']:
                    raise Error('确认后平台信息已改变，跳过该项')
                for step in item['steps']:
                    if step['kind'] == 'script':
                        agent.trusted_local_script(Path(step['path']), step['sha256'])
                    elif step['kind'] == 'binary':
                        trusted_binary(Path(step['path']), step['sha256'])
                    elif step['kind'] == 'package':
                        if package_step(ctx, step['identifier'])['argv'] != step['argv']:
                            raise Error('确认后软件包事务已改变')
                if item['id'] == 'cloud-init':
                    network = persistent_network(ctx)
                    if not network or any(not Path(p).is_file() or Path(p).is_symlink() or digest(p) != item['network_hashes'][p]
                                          for p in item['network_files']):
                        raise Error('确认后网络配置已改变，跳过该项')
                    for path in network:
                        backup_file(ctx, path)
                    for path in [ctx.path('/etc/ssh/sshd_config')] + list(ctx.path('/etc/ssh/sshd_config.d').glob('*.conf')):
                        if path.is_file() and not path.is_symlink():
                            backup_file(ctx, path)
                disabled_only = False
                for step in item['steps']:
                    if step['kind'] == 'disable_cloud_init':
                        disable_cloud_init(ctx)
                        disabled_only = True
                        continue
                    if step['kind'] == 'cleanup_assist':
                        cleanup_assist(ctx)
                        continue
                    if step['kind'] == 'script':
                        agent.trusted_local_script(Path(step['path']), step['sha256'])
                    elif step['kind'] == 'package':
                        # Re-run the dependency check; never use --force or --nodeps.
                        if package_step(ctx, step['identifier'])['argv'] != step['argv']:
                            raise Error('确认后软件包事务已改变')
                    elif step['kind'] not in ('binary', 'service'):
                        raise Error('未知执行步骤')
                    response = ctx.execute(step['argv'], timeout=300,
                                           cwd=str(Path(step['path']).parent) if step['kind'] in ('script', 'binary') else None,
                                           interactive=bool(component.get('protected') and sys.stdin.isatty()))
                    result['output'] += redact(response.stdout + response.stderr)[-5000:]
                    if response.returncode and step['kind'] != 'service':
                        if item['id'] == 'cloud-init' and item.get('fallback_disable'):
                            disable_cloud_init(ctx)
                            disabled_only = True
                            result['reason'] = '包卸载未完成；已禁用下次启动，配置和日志保留'
                        else:
                            raise Error('官方卸载程序返回 {}'.format(response.returncode))
                after = agent.audit(ctx)
                observed = next(c for c in after['components'] if c['id'] == item['id'])
                result['verified'] = observed
                if disabled_only:
                    result['status'] = 'disabled_only'
                elif item['id'] in ('cloud-init', 'aliyun-assist'):
                    absent = not package_installed(ctx, item['id']) and not observed.get('processes')
                    result['status'] = 'removed' if absent and not after['limitations'] else 'residual'
                    result['reason'] = '已移除程序包；配置、初始化历史及日志按需保留' if result['status'] == 'removed' else '仍有运行/包证据，或检查信息不完整'
                else:
                    absent = not any(observed.get(k) for k in ('paths', 'processes', 'services', 'packages'))
                    result['status'] = 'removed' if absent and not after['limitations'] else 'residual'
                    if result['status'] == 'residual':
                        result['reason'] = '官方程序已返回；仍有残留或检查信息不完整，请核对发现依据'
            except KeyboardInterrupt:
                result.update(status='interrupted', reason='用户中断；其余项目未执行')
                report['returncode'] = 1
                write_json(folder / 'latest.json', report)
                break
            except (Error, OSError, ValueError) as exc:
                result.update(status='failed', reason=redact(exc))
            if result['status'] != 'removed':
                report['returncode'] = 1
            write_json(folder / 'latest.json', report)
        processed = {item['id'] for item in report['results']}
        report['results'] += [{'id': item['id'], 'status': 'not_run', 'reason': '批次已中断，未执行该项', 'output': ''}
                              for item in approved['entries'] if item['id'] not in processed]
        report['finished'] = dt.datetime.now(dt.timezone.utc).isoformat()
        ctx.event('agent.batch.end', returncode=report['returncode'])
        write_json(folder / 'latest.json', report)
    return report
