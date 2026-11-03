import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from . import __version__, agent, backup, forwarder, health, traffic
from .core import Context, Error, atomic, configuration, confirm, read_json, save_config, write_json
from .platform import detect, service


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def parser():
    p = argparse.ArgumentParser(description='VPS Toolkit：Linux 环境、Agent 官方管理、PF/Realm、授权下行测试')
    p.add_argument('--version', action='version', version='vps-toolkit ' + __version__)
    p.add_argument('--root', default='/', help='离线检查根目录；不允许执行宿主机服务操作')
    sub = p.add_subparsers(dest='module')
    for name in ('info', 'health', 'network', 'wizard'):
        sub.add_parser(name)
    cloud = sub.add_parser('cloud').add_subparsers(dest='action', required=True)
    cloud.add_parser('show')
    select = cloud.add_parser('select')
    select.add_argument('provider', choices=['aliyun', 'tencent', 'huawei', 'baidu', 'ucloud', 'jdcloud', 'generic'])
    agents = sub.add_parser('agent').add_subparsers(dest='action', required=True)
    for name in ('audit', 'verify', 'report'):
        q = agents.add_parser(name)
        if name == 'report':
            q.add_argument('--output')
    q = agents.add_parser('remove')
    q.add_argument('component', choices=sorted(agent.CATALOG))
    q.add_argument('--dry-run', action='store_true')
    q.add_argument('--confirm')
    q.add_argument('--console-authorized', action='store_true')
    q.add_argument('--confirm-network-independent', action='store_true')
    q.add_argument('--official-script')
    q.add_argument('--sha256')
    pf = sub.add_parser('pf').add_subparsers(dest='action', required=True)
    for name in ('install', 'repair'):
        q = pf.add_parser(name)
        q.add_argument('--archive')
        q.add_argument('--restart', action='store_true')
    for name in ('status', 'diagnose', 'legacy'):
        pf.add_parser(name)
    q = pf.add_parser('service')
    q.add_argument('operation', choices=['start', 'stop', 'restart', 'enable', 'disable', 'status', 'logs'])
    rules = pf.add_parser('rule').add_subparsers(dest='rule_action', required=True)
    rules.add_parser('list')
    for name in ('add', 'edit', 'delete'):
        q = rules.add_parser(name)
        if name != 'add':
            q.add_argument('index', type=int)
        if name != 'delete':
            q.add_argument('--listen', required=name == 'add')
            q.add_argument('--remote', required=name == 'add')
            q.add_argument('--protocol', choices=['tcp', 'udp', 'both'], default='both')
        q.add_argument('--apply', action='store_true', help='显式重启 Realm 应用规则，失败回滚')
    for name in ('import', 'export'):
        q = pf.add_parser(name)
        q.add_argument('file')
        if name == 'import':
            q.add_argument('--apply', action='store_true')
    t = sub.add_parser('traffic').add_subparsers(dest='action', required=True)
    for name in ('status', 'tick', 'pause', 'stop', 'disable'):
        t.add_parser(name)
    q = t.add_parser('resume')
    q.add_argument('--job')
    for name in ('download', 'test'):
        q = t.add_parser(name)
        size = q.add_mutually_exclusive_group()
        size.add_argument('--mb', type=float)
        size.add_argument('--gb', type=float)
        q.add_argument('--url')
        q.add_argument('--mbps', type=float)
        q.add_argument('--max-runtime', type=int)
        q.add_argument('--sha256')
        q.add_argument('--ignore-ratio', action='store_true', help='显式手动实测忽略比例缺口，仍遵守日/月预算')
    q = t.add_parser('authorize')
    q.add_argument('url')
    q.add_argument('--remove', action='store_true')
    q = t.add_parser('configure')
    for flag in ('daily-gb', 'monthly-gb', 'task-gb', 'mbps', 'link-mbps', 'reserve-mbps'):
        q.add_argument('--' + flag, type=float)
    for flag in ('max-runtime', 'max-concurrency'):
        q.add_argument('--' + flag, type=int)
    for flag in ('timezone', 'times', 'window', 'hourly-weights'):
        q.add_argument('--' + flag)
    q.add_argument('--region', choices=['cn', 'overseas'])
    q.add_argument('--strategy', choices=['quota', 'ratio'])
    q.add_argument('--ratio', help='上行:下行，例如 1:2')
    q.add_argument('--ratio-interface', help='auto 或出口网卡名称，多个用逗号分隔')
    q.add_argument('--ratio-schedule', choices=['follow', 'random'])
    q.add_argument('--ratio-tolerance-pct', type=float)
    q = t.add_parser('ratio-seed', help='补录当前累计上下行流量；不清零下载预算')
    q.add_argument('--upload-gb', type=float, required=True)
    q.add_argument('--download-gb', type=float, required=True)
    q = t.add_parser('enable')
    q.add_argument('--replace-legacy', action='store_true')
    backups = sub.add_parser('backup').add_subparsers(dest='action', required=True)
    q = backups.add_parser('create')
    q.add_argument('--output')
    q = backups.add_parser('restore')
    q.add_argument('file')
    q.add_argument('--apply', action='store_true')
    q.add_argument('--confirm')
    sub.add_parser('logs')
    q = sub.add_parser('update')
    q.add_argument('--check', action='store_true')
    q.add_argument('--installer')
    q.add_argument('--sha256')
    q = sub.add_parser('uninstall')
    q.add_argument('--confirm')
    return p


def download_child(ctx, args):
    if os.name == 'posix' and os.geteuid() == 0 and ctx.root == Path('/'):
        import pwd
        try:
            account = pwd.getpwnam('traffic-scheduler')
        except KeyError:
            raise Error('请先运行安装器建立独立下载账户')
        command = ['runuser', '-u', account.pw_name, '--', sys.executable,
                   str(Path(__file__).resolve().parents[1] / 'run.py'), 'traffic', args.action]
        for key in ('job', 'mb', 'gb', 'url', 'mbps', 'max_runtime', 'sha256'):
            value = getattr(args, key, None)
            if value is not None:
                command += ['--' + key.replace('_', '-'), str(value)]
        if getattr(args, 'ignore_ratio', False):
            command.append('--ignore-ratio')
        return {'returncode': subprocess.call(command)}
    return None


def enable_traffic(ctx, config, replace_legacy=False):
    init = detect(ctx)['init']
    if init not in ('systemd', 'openrc'):
        raise Error('没有可用的 systemd/OpenRC，配置与原任务状态保持不变')
    old = 'traffic-scheduler.timer' if init == 'systemd' else 'traffic-scheduler'
    new = 'vps-toolkit-traffic.timer' if init == 'systemd' else 'vps-toolkit-traffic'
    snapshots = {name: {state: service(ctx, name, state).returncode == 0
                        for state in ('is-enabled', 'is-active')} for name in (old, new)}
    if any(snapshots[old].values()) and not replace_legacy:
        raise Error('原定时任务仍启用或运行，已保留其状态；确认替换时使用 --replace-legacy')
    previous = json.loads(json.dumps(config))
    config['traffic']['enabled'] = True
    activated = False
    try:
        save_config(ctx, config)
        # Keep the old scheduler alive until the new timer is successfully activated.
        service(ctx, new, 'enable', check=True)
        service(ctx, new, 'start', check=True)
        activated = True
        if any(snapshots[old].values()):
            service(ctx, old, 'stop', check=True)
            service(ctx, old, 'disable', check=True)
    except Exception:
        save_config(ctx, previous)
        for name in (new, old) if activated else (new,):
            for state, yes, no in [('is-enabled', 'enable', 'disable'), ('is-active', 'start', 'stop')]:
                try:
                    service(ctx, name, yes if snapshots[name][state] else no)
                except Error:
                    pass
        raise


def traffic_dispatch(ctx, args):
    action = args.action
    if action == 'status':
        return traffic.status(ctx)
    if action == 'resume' and args.job and os.name == 'posix' and os.geteuid() == 0:
        ctx.require_root()
        traffic.set_control(ctx, 'resume')
    if action in ('download', 'test', 'tick') or action == 'resume' and args.job:
        child = download_child(ctx, args)
        if child is not None:
            return child
        if action == 'tick':
            return traffic.tick(ctx)
        if action == 'resume':
            return traffic.download(ctx, job_id=args.job)
        value = args.gb * 1e9 if args.gb is not None else args.mb * 1e6 if args.mb is not None else None
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise Error('任务下载量必须是有限正数')
        return traffic.download(ctx, size=int(value) if value is not None else None, mbps=args.mbps,
                                url=args.url, max_runtime=args.max_runtime, expected_sha256=args.sha256,
                                ignore_ratio=args.ignore_ratio)
    ctx.require_root()
    if action in ('pause', 'stop', 'resume'):
        traffic.set_control(ctx, action)
        if action == 'stop':
            try:
                service(ctx, 'traffic-scheduler', 'stop')
            except Error:
                pass
        return {'action': action, 'note': '不操作 Realm；停止/暂停在当前有限大小请求结束后生效'}
    config = configuration(ctx)
    item = config['traffic']
    if action == 'ratio-seed':
        values = (args.upload_gb, args.download_gb)
        if any(not math.isfinite(v) or v < 0 for v in values):
            raise Error('累计 GB 须为有限非负数')
        with traffic.engine.locked(ctx.path(item['state_dir'])):
            return traffic.ratio.seed(ctx, item, int(values[0] * 1e9), int(values[1] * 1e9))
    if action == 'authorize':
        if args.remove:
            item['authorized_urls'] = [u for u in item['authorized_urls'] if u != args.url]
        elif args.url not in item['authorized_urls']:
            item['authorized_urls'].append(args.url)
    elif action == 'configure':
        for name in ('daily', 'monthly', 'task'):
            value = getattr(args, name + '_gb')
            if value is not None:
                if not math.isfinite(value) or value <= 0:
                    raise Error('流量预算须为有限正数')
                item[name + '_bytes'] = int(value * 1e9)
        for name in ('mbps', 'link_mbps', 'reserve_mbps', 'max_runtime', 'max_concurrency', 'region',
                     'strategy', 'ratio', 'ratio_interface', 'ratio_schedule', 'ratio_tolerance_pct'):
            value = getattr(args, name)
            if value is not None:
                item[name] = value
        if args.timezone is not None and args.timezone != item['timezone']:
            folder = ctx.path(item['state_dir'])
            if any((read_json(p) or {}).get('download', {}).get('bytes', 0) for p in folder.glob('quota-*.json')):
                raise Error('已有流量统计时不自动更换日期时区，请保留原时区以避免重复预算')
            item['timezone'] = args.timezone
        if args.times is not None:
            item['times'] = [v.strip() for v in args.times.split(',') if v.strip()]
        if args.window is not None:
            item['window'] = args.window.split('-')
        if args.hourly_weights is not None:
            item['hourly_weights'] = (list(traffic.engine.DEFAULT_HOURLY_WEIGHTS) if args.hourly_weights == 'estimated'
                                      else [1] * 24 if args.hourly_weights == 'uniform'
                                      else [float(v) for v in args.hourly_weights.split(',')])
    elif action in ('enable', 'disable'):
        if action == 'enable':
            if not item['authorized_urls']:
                raise Error('自动下载默认关闭，启用前必须先授权测试 URL')
            enable_traffic(ctx, config, args.replace_legacy)
            return {'saved': str(ctx.config_path), 'traffic': item, 'existing_daily_counts_preserved': True}
        else:
            item['enabled'] = False
            names = ('vps-toolkit-traffic.timer', 'traffic-scheduler.timer') if detect(ctx)['init'] == 'systemd' else ('vps-toolkit-traffic', 'traffic-scheduler')
            for name in names:
                try:
                    service(ctx, name, 'disable')
                    service(ctx, name, 'stop')
                except Error:
                    pass
    save_config(ctx, config)
    return {'saved': str(ctx.config_path), 'traffic': item, 'existing_daily_counts_preserved': True}


def dispatch(ctx, args):
    if args.module in ('info', 'health', 'network'):
        return health.report(ctx)
    if args.module == 'cloud':
        if args.action == 'select':
            ctx.require_root()
            config = configuration(ctx)
            config['cloud_override'] = args.provider
            save_config(ctx, config)
        return health.identify_cloud(ctx, configuration(ctx).get('cloud_override'))
    if args.module == 'agent':
        if args.action == 'remove':
            return agent.remove(ctx, args.component, dry_run=args.dry_run, confirmation=args.confirm,
                                console_authorized=args.console_authorized, script=args.official_script,
                                sha256=args.sha256, network_independent=args.confirm_network_independent)
        result = agent.audit(ctx)
        if args.action == 'report' and args.output:
            write_json(Path(args.output), result)
        return result
    if args.module == 'traffic':
        return traffic_dispatch(ctx, args)
    if args.module == 'pf':
        if args.action in ('status', 'diagnose'):
            return forwarder.diagnostics(ctx)
        if args.action == 'legacy':
            path = ctx.path('/usr/local/bin/pf')
            if not path.exists() or 'VPS_TOOLKIT_MANAGED' in path.read_text(errors='replace')[:300]:
                raise Error('未发现独立旧 PF 入口；直接使用 pf rule / pf service')
            ctx.require_root()
            return {'returncode': subprocess.call([str(path)])}
        if args.action == 'rule' and args.rule_action == 'list':
            return forwarder.rule(ctx, 'list')
        if args.action == 'export':
            settings = configuration(ctx)['forwarder']
            atomic(Path(args.file), ctx.path(settings['config']).read_bytes())
            return {'exported': args.file}
        ctx.require_root()
        if args.action in ('install', 'repair'):
            return forwarder.install(ctx, archive=args.archive, restart=args.restart)
        if args.action == 'service':
            result = service(ctx, configuration(ctx)['forwarder']['service'], args.operation, check=True)
            return {'output': result.stdout}
        if args.action == 'import':
            return forwarder.save(ctx, json.loads(Path(args.file).read_text(encoding='utf-8')), args.apply)
        if args.action == 'rule':
            return forwarder.rule(ctx, args.rule_action, index=getattr(args, 'index', None),
                                  listen=getattr(args, 'listen', None), remote=getattr(args, 'remote', None),
                                  protocol=getattr(args, 'protocol', 'both'), apply=args.apply)
    if args.module == 'backup':
        ctx.require_root()
        return backup.create(ctx, args.output) if args.action == 'create' else backup.restore(ctx, args.file, not args.apply, args.confirm)
    if args.module == 'logs':
        path = ctx.path('/var/log/vps-toolkit/events.jsonl')
        return {'events': path.read_text(errors='replace').splitlines()[-80:] if path.exists() else []}
    if args.module in ('update', 'uninstall'):
        from .maintenance import update, uninstall
        return update(ctx, args) if args.module == 'update' else uninstall(ctx, args.confirm)
    if args.module == 'wizard':
        return wizard(ctx)
    return menu(ctx)


def wizard(ctx):
    output(health.report(ctx))
    output(agent.audit(ctx))
    print('操作计划：保留既有服务、配置和任务状态；Agent 卸载、Realm 重启和新定时下载均单独确认。')
    if input('是否进入 Agent 官方卸载菜单？[y/N]: ').strip().lower() == 'y':
        identifier = input('组件名（空白跳过）: ').strip()
        if identifier:
            output(agent.remove(ctx, identifier, dry_run=True))
            print('先核实官方文档和程序 SHA256，再执行 agent remove；不会在向导中自动卸载。')
    if input('安装/修复 Realm 二进制？不重启现有服务。[y/N]: ').strip().lower() == 'y':
        ctx.require_root()
        output(forwarder.install(ctx))
    if input('进行手动下载测试？将计入预算。[y/N]: ').strip().lower() == 'y':
        url = input('已获授权的测试 URL: ').strip()
        if url:
            main(['traffic', 'authorize', url], ctx)
            main(['traffic', 'download', '--mb', '10', '--url', url], ctx)
    if input('明确启用新下载定时任务？[y/N]: ').strip().lower() == 'y':
        main(['traffic', 'enable'], ctx)
    return {'acceptance': health.report(ctx), 'backup': backup.create(ctx)}


def menu(ctx):
    labels = ['查看系统信息', '查看云平台信息', '检查客户机 Agent', '管理可选 Agent 官方卸载', '验证卸载结果',
              '安装/修复 PF', 'Realm 转发规则管理', 'Realm 服务管理', '手动下行下载测试', '下载任务调度',
              '流量统计', '网络诊断', 'SSH 与网络检查', '配置备份', '配置恢复', '查看日志', '更新管理工具',
              '卸载管理工具', '退出']
    while True:
        print('\nVPS Toolkit ' + __version__)
        for index, label in enumerate(labels, 1):
            print('{}. {}'.format(index, label))
        choice = input('请选择: ').strip()
        if choice == '19':
            return {'exit': True}
        commands = {'1': ['info'], '2': ['cloud', 'show'], '3': ['agent', 'audit'], '5': ['agent', 'verify'],
                    '6': ['pf', 'repair'], '11': ['traffic', 'status'], '12': ['network'], '13': ['health'],
                    '14': ['backup', 'create'], '16': ['logs'], '17': ['update', '--check']}
        if choice == '4':
            component = input('组件（{}）: '.format(', '.join(agent.CATALOG))).strip()
            if main(['agent', 'remove', component, '--dry-run'], ctx):
                continue
            if input('已核实上述官方计划，进入卸载确认？[y/N]: ').strip().lower() != 'y':
                continue
            command = ['agent', 'remove', component]
            if component == 'cloud-init':
                if input('确认网络和 SSH 已独立持久化，请输入 network-independent: ').strip() != 'network-independent':
                    print('已取消')
                    continue
                command.append('--confirm-network-independent')
            else:
                script = input('官方卸载程序路径（留空使用已检测路径）: ').strip()
                if script:
                    command += ['--official-script', script]
                command += ['--sha256', input('从可信官方发布物核实的程序 SHA256: ').strip()]
                if agent.CATALOG[component].get('protected'):
                    if input('已按官方流程完成控制台卸载授权？[y/N]: ').strip().lower() != 'y':
                        print('已取消')
                        continue
                    command.append('--console-authorized')
            # remove() still requires the component-specific final confirmation.
            commands[choice] = command
        elif choice == '7':
            operation = input('list / add / edit / delete [list]: ').strip() or 'list'
            command = ['pf', 'rule', operation]
            if operation in ('edit', 'delete'):
                command.append(input('规则序号（从0开始）: ').strip())
            if operation in ('add', 'edit'):
                command += ['--listen', input('监听 IP:端口: ').strip(), '--remote', input('目标地址:端口: ').strip(),
                            '--protocol', input('tcp/udp/both [both]: ').strip() or 'both']
            if operation != 'list' and input('保存后显式重启 Realm 应用？[y/N]: ').strip().lower() == 'y':
                command.append('--apply')
            commands[choice] = command
        elif choice == '8':
            commands[choice] = ['pf', 'service', input('start/stop/restart/enable/disable/status/logs: ').strip()]
        elif choice == '9':
            url = input('测试 URL: ').strip()
            main(['traffic', 'authorize', url], ctx)
            commands[choice] = ['traffic', 'download', '--url', url, '--mb', input('下载 MB [10]: ').strip() or '10']
        elif choice == '10':
            operation = input('status / enable / disable / pause / resume / stop / configure / ratio / ratio-seed: ').strip()
            command = ['traffic', operation]
            if operation == 'configure':
                command += ['--daily-gb', input('每日预算 GB: ').strip(), '--monthly-gb', input('每月预算 GB: ').strip(),
                            '--mbps', input('下载限速 Mbps: ').strip()]
            elif operation == 'ratio':
                command = ['traffic', 'configure', '--strategy', 'ratio',
                           '--ratio', input('上行:下行 [1:2]: ').strip() or '1:2',
                           '--ratio-interface', input('出口网卡 [auto]: ').strip() or 'auto',
                           '--ratio-schedule', input('follow 实时补 / random 随机补 [follow]: ').strip() or 'follow']
            elif operation == 'ratio-seed':
                command += ['--upload-gb', input('当前全部累计上行 GB: ').strip(),
                            '--download-gb', input('当前全部累计下行 GB: ').strip()]
            commands[choice] = command
        elif choice == '15':
            commands[choice] = ['backup', 'restore', input('备份路径: ').strip(), '--apply']
        elif choice == '18':
            commands[choice] = ['uninstall']
        elif choice == '17':
            if main(['update', '--check'], ctx):
                continue
            if input('下载验证并更新管理工具？[y/N]: ').strip().lower() != 'y':
                continue
            commands[choice] = ['update']
        if choice in commands:
            try:
                main(commands[choice], ctx)
            except (Error, ValueError) as exc:
                print('操作失败: ' + str(exc))


def main(argv=None, ctx=None):
    args = parser().parse_args(argv)
    context = ctx or Context(args.root)
    try:
        result = dispatch(context, args)
        output(result)
        if isinstance(result, dict) and (result.get('returncode', 0) != 0 or result.get('status') in ('failed', 'budget_exhausted', 'interrupted')):
            return 1
        return 0
    except (Error, OSError, ValueError, KeyboardInterrupt) as exc:
        print('失败: ' + str(exc), file=sys.stderr)
        return 1
