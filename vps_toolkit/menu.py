"""Numbered terminal pages. A result stays visible until the user enters 0."""
import sys
from . import __version__, agent
from .core import Error, configuration
from .presentation import AGENTS, clean


class Back(Exception):
    pass


class EndMenu(Exception):
    pass


def clear_screen():
    if sys.stdout.isatty():
        print('\033[2J\033[H', end='', flush=True)


def screen(title):
    clear_screen()
    print('═' * 48)
    print('  VPS Toolkit {}  /  {}'.format(__version__, title))
    print('═' * 48)
    print()


def ask(label, default=None):
    suffix = ' [{}]'.format(default) if default is not None else ''
    try:
        value = input(label + suffix + '：').strip()
    except EOFError:
        raise EndMenu()
    return value or (str(default) if default is not None else '')


def choose(title, choices, exit_label='返回上一级', aliases=None):
    message = ''
    while True:
        screen(title)
        for key, label in choices:
            print('  {:>2}. {}'.format(key, label))
        print('\n   0. ' + exit_label)
        if message:
            print('\n  ' + message)
        choice = ask('请选择数字')
        if aliases:
            choice = aliases.get(choice, choice)
        if choice == '0':
            return None
        if choice in {key for key, _ in choices}:
            return choice
        message = '输入无效，请选择页面列出的数字。'


def wait_back():
    print('\n' + '─' * 48)
    while True:
        if ask('输入 0 返回上一级') == '0':
            return
        print('  结果仍保留在此页；请输入 0 返回。')


def run(ctx, command):
    from .cli import main
    return main(command, ctx, human=True)


def action_page(ctx, title, action):
    screen(title)
    try:
        if action() is False:
            return
    except Back:
        return
    except KeyboardInterrupt:
        print('\n操作已取消。')
    except (Error, OSError, ValueError) as exc:
        print('\n操作失败：' + clean(exc))
    wait_back()


def command_page(ctx, title, command):
    action_page(ctx, title, lambda: run(ctx, command))


def submenu(ctx, title, entries):
    while True:
        choice = choose(title, [(key, label) for key, label, _ in entries])
        if choice is None:
            return
        _, label, action = next(entry for entry in entries if entry[0] == choice)
        action_page(ctx, label, action)


def agent_remove(ctx):
    components = list(agent.CATALOG)
    choice = choose('选择要管理的 Agent', [(str(i + 1), AGENTS.get(name, name)) for i, name in enumerate(components)])
    if choice is None:
        raise Back()
    component = components[int(choice) - 1]
    screen(AGENTS.get(component, component) + ' · 官方卸载计划')
    if run(ctx, ['agent', 'remove', component, '--dry-run']):
        return
    if ask('已核实上述官方计划，进入卸载确认？y/N', 'N').lower() != 'y':
        print('已取消。')
        return
    command = ['agent', 'remove', component]
    if component == 'cloud-init':
        if ask('确认网络与 SSH 已独立持久化，请输入 network-independent') != 'network-independent':
            print('已取消。')
            return
        command.append('--confirm-network-independent')
    else:
        script = ask('官方卸载程序路径（留空使用已检测路径）')
        if script:
            command += ['--official-script', script]
        command += ['--sha256', ask('从可信官方发布物核实的程序 SHA256')]
        if agent.CATALOG[component].get('protected'):
            if ask('已按官方流程完成控制台卸载授权？y/N', 'N').lower() != 'y':
                print('已取消。')
                return
            command.append('--console-authorized')
    run(ctx, command)


def remove_all(ctx, scope='all'):
    from . import agent_batch
    from .presentation import render
    initial = agent_batch.plan(ctx, scope=scope)
    detected = {item['id'] for item in initial['entries'] if item['status'] not in ('not_detected', 'excluded')}
    independent = False
    authorized = False
    if detected & {'cloud-init', 'aliyun-assist', 'tat'}:
        print('云助手卸载可能中断控制台免密会话；cloud-init 负责部分初始化和网络配置。')
        independent = ask('已通过独立 SSH/VNC 登录，并核实网络和 SSH 配置已持久化？y/N', 'N').lower() == 'y'
    if detected & {'aegis', 'yunjing'}:
        print('安全客户端需按官方流程完成自保护/卸载授权，官方程序可能要求验证码。')
        authorized = ask('已为本次检测到的安全客户端完成控制台卸载授权？y/N', 'N').lower() == 'y'
    approved = agent_batch.plan(ctx, scope=scope, console_authorized=authorized, network_independent=independent)
    screen('批量卸载 · 本次执行清单')
    render(approved)
    if not any(item['status'] == 'ready' for item in approved['entries']):
        print('\n暂无可直接执行的项目，请按每项原因处理后重试。')
        return
    answer = ask('确认按上方清单执行，请输入 remove-all（其他输入取消）')
    if answer != 'remove-all':
        print('已取消，未执行卸载。')
        return
    print('\n开始逐项卸载，完成后显示每项结果……\n')
    render(agent_batch.execute(ctx, approved, confirmation=answer))


def agent_audit(ctx):
    while True:
        screen('Agent 检测与批量卸载')
        if run(ctx, ['agent', 'audit']):
            return
        print('\n  1. 一键卸载全部已检测 Agent（含 cloud-init）\n  2. 仅卸载监控 Agent\n  0. 返回上一级')
        choice = ask('请选择数字')
        if choice == '0':
            return False
        if choice in ('1', '2'):
            action_page(ctx, '批量卸载 Agent', lambda: remove_all(ctx, 'all' if choice == '1' else 'monitoring'))


def agent_menu(ctx):
    submenu(ctx, 'Agent 卸载管理', [
        ('1', '一键卸载全部已检测 Agent（含 cloud-init）', lambda: remove_all(ctx)),
        ('2', '仅卸载监控 Agent', lambda: remove_all(ctx, 'monitoring')),
        ('3', '单项官方卸载', lambda: agent_remove(ctx)),
        ('4', '查看上次批量卸载报告', lambda: run(ctx, ['agent', 'removal-report']))])


def edit_rule(ctx, operation):
    if run(ctx, ['pf', 'rule', 'list']):
        return
    command = ['pf', 'rule', operation]
    if operation in ('edit', 'delete'):
        command.append(ask('规则序号（从 0 开始）'))
    if operation in ('add', 'edit'):
        for flag, label in (('listen', '监听 IP:端口'), ('remote', '目标地址:端口')):
            value = ask(label + ('（留空保留）' if operation == 'edit' else ''))
            if value or operation == 'add':
                command += ['--' + flag, value]
        protocol = ask('协议：1 TCP / 2 UDP / 3 TCP+UDP', '3')
        if protocol not in ('1', '2', '3'):
            raise Error('协议请选择 1、2 或 3')
        command += ['--protocol', {'1': 'tcp', '2': 'udp', '3': 'both'}[protocol]]
    if ask('保存后重启 Realm 应用规则？y/N', 'N').lower() == 'y':
        command.append('--apply')
    run(ctx, command)


def rule_menu(ctx):
    submenu(ctx, 'Realm 转发规则', [
        ('1', '查看规则', lambda: run(ctx, ['pf', 'rule', 'list'])),
        ('2', '添加规则', lambda: edit_rule(ctx, 'add')),
        ('3', '修改规则', lambda: edit_rule(ctx, 'edit')),
        ('4', '删除规则', lambda: edit_rule(ctx, 'delete'))])


def service_menu(ctx):
    entries = [('1', '查看服务状态', 'status'), ('2', '启动服务', 'start'), ('3', '停止服务', 'stop'),
               ('4', '重启服务', 'restart'), ('5', '开启开机启动', 'enable'),
               ('6', '关闭开机启动', 'disable'), ('7', '查看服务日志', 'logs')]
    submenu(ctx, 'Realm 服务管理', [(key, label, lambda operation=operation: run(ctx, ['pf', 'service', operation]))
                                  for key, label, operation in entries])


def download(ctx):
    item = configuration(ctx)['traffic']
    urls = item['authorized_urls']
    print('手动下载计入日/月预算，临时内容自动清理。\n')
    if urls:
        print('当前默认资源：' + clean(urls[0]))
    url = ask('下载 URL（留空使用当前默认资源）')
    if url and run(ctx, ['traffic', 'authorize', url]):
        return
    if not url and not urls:
        raise Error('尚无授权资源，请填写可下载的文件 URL')
    command = ['traffic', 'download', '--mb', ask('本次下载 MB', '10')]
    if url:
        command += ['--url', url]
    if item.get('strategy') == 'ratio':
        if ask('即使比例已满足也执行此次手动实测？y/N', 'N').lower() == 'y':
            command.append('--ignore-ratio')
    print('\n正在执行下载，请等待结果……\n')
    run(ctx, command)


def configure_quota(ctx):
    item = configuration(ctx)['traffic']
    print('仅修改补下载的预算上限与限速，保留当前模式：' + ('累计比例补下行' if item['strategy'] == 'ratio' else '固定额度') + '。\n')
    command = ['traffic', 'configure']
    for flag, label, default in [('daily-gb', '每日补下载上限 GB', item['daily_bytes'] / 1e9),
                                 ('monthly-gb', '每月补下载上限 GB', item['monthly_bytes'] / 1e9),
                                 ('mbps', '速度上限 Mbps', item['mbps'])]:
        command += ['--' + flag, ask(label, default)]
    run(ctx, command)


def configure_strategy(ctx):
    choice = choose('切换下载模式', [('1', '固定额度：按预算随机分块下载'), ('2', '累计比例：仅补上行:下行缺口')])
    if choice is None:
        raise Back()
    if choice == '2':
        configure_ratio(ctx)
    else:
        run(ctx, ['traffic', 'configure', '--strategy', 'quota'])


def configure_ratio(ctx):
    from . import ratio, traffic
    item = configuration(ctx)['traffic']
    while True:
        supplied = ask('目标比例：上行:下行（0 返回）', item.get('ratio', '1:2'))
        if supplied == '0':
            raise Back()
        try:
            normalized = ratio.normalize(supplied)
            break
        except Error as exc:
            print('输入有误：' + clean(exc) + '；可直接回车采用默认值，或输入 0 返回。')
    command = ['traffic', 'configure', '--strategy', 'ratio', '--ratio',
               normalized,
               '--ratio-interface', ask('计量出口网卡', item.get('ratio_interface', 'auto')),
               '--mbps', ask('补下载限速 Mbps', item['mbps'])]
    schedule = ask('调度：1 按分钟跟随 / 2 全天随机分散', '1' if item.get('ratio_schedule', 'follow') == 'follow' else '2')
    if schedule not in ('1', '2'):
        raise Error('调度请选择 1 或 2')
    command += ['--ratio-schedule', 'follow' if schedule == '1' else 'random']
    if run(ctx, command):
        return
    readiness = traffic.initialize_meter(ctx)
    if readiness['status'] == 'unavailable' and readiness.get('manual_interface'):
        print('\n无法自动读取整机流量：' + clean(readiness['note']))
        interface = ask('请手动指定出口网卡（如 eth0；0 返回）', '0')
        if interface == '0':
            raise Back()
        run(ctx, ['traffic', 'configure', '--ratio-interface', interface])


def configure_time(ctx):
    item = configuration(ctx)['traffic']
    command = ['traffic', 'configure', '--window', ask('允许时段 HH:MM-HH:MM', '-'.join(item['window']))]
    times = ask('固定时刻，逗号分隔；输入 - 清除', ','.join(item['times']) or '-')
    command += ['--times', '' if times == '-' else times]
    weight = ask('小时权重：1 保留 / 2 估算作息 / 3 均匀', '1')
    if weight not in ('1', '2', '3'):
        raise Error('权重请选择 1、2 或 3')
    if weight != '1':
        command += ['--hourly-weights', 'estimated' if weight == '2' else 'uniform']
    run(ctx, command)


def configure_download(ctx):
    item = configuration(ctx)['traffic']
    print('所有连接共用总限速；测速仅下行，数据自动清理，仍计入日/月预算。\n')
    mode = ask('方式：1 文件分段 / 2 仅下行测速', '2' if item.get('download_mode') == 'speedtest' else '1')
    if mode not in ('1', '2'):
        raise Error('方式请选择 1 或 2')
    run(ctx, ['traffic', 'configure', '--download-mode', 'file' if mode == '1' else 'speedtest',
              '--max-concurrency', ask('最大连接数（1–32，建议 4）', item['max_concurrency']),
              '--mbps', ask('所有连接合计限速 Mbps（运行中修改生效）', item['mbps'])])


def configure_batches(ctx):
    item = configuration(ctx)['traffic']
    print('按总限速计算多连接分块；每批完成后随机等待，再继续下载。\n')
    run(ctx, ['traffic', 'configure', '--batch-seconds', ask('每批目标下载秒数（1–30）', item.get('batch_seconds', 8)),
              '--cooldown-min-seconds', ask('随机等待最少秒数', item.get('cooldown_min_seconds', 5)),
              '--cooldown-max-seconds', ask('随机等待最多秒数', item.get('cooldown_max_seconds', 10))])


def ratio_seed(ctx):
    print('输入此刻整机的累计总量，含已有历史；不是要增加的差值。\n不会清零本工具日/月已用额度。\n')
    run(ctx, ['traffic', 'ratio-seed', '--upload-gb', ask('累计上行 GB'), '--download-gb', ask('累计下行 GB')])


def enable_traffic(ctx):
    command = ['traffic', 'enable']
    if ask('同时替换仍启用的旧 traffic-scheduler 入口？y/N', 'N').lower() == 'y':
        command.append('--replace-legacy')
    run(ctx, command)


def resource(ctx, remove=False):
    for index, url in enumerate(configuration(ctx)['traffic']['authorized_urls'], 1):
        print('  {}. {}'.format(index, clean(url)))
    command = ['traffic', 'authorize', ask('要移除的完整 URL' if remove else '已获授权的下载 URL')]
    if remove:
        command.append('--remove')
    run(ctx, command)


def traffic_menu(ctx):
    entries = [('1', '查看设置、用量与比例缺口', lambda: run(ctx, ['traffic', 'status'])),
               ('2', '启用自动任务', lambda: enable_traffic(ctx)),
               ('3', '暂停下载', lambda: run(ctx, ['traffic', 'pause'])),
               ('4', '解除暂停', lambda: run(ctx, ['traffic', 'resume'])),
               ('5', '停止当前下载', lambda: run(ctx, ['traffic', 'stop'])),
               ('6', '关闭自动任务', lambda: run(ctx, ['traffic', 'disable'])),
               ('7', '下载额度与限速（保留当前模式）', lambda: configure_quota(ctx)),
               ('8', '累计上下行比例设置', lambda: configure_ratio(ctx)),
               ('9', '补录 / 校准整机历史累计流量', lambda: ratio_seed(ctx)),
               ('10', '时间窗口与随机权重', lambda: configure_time(ctx)),
               ('11', '恢复指定下载任务', lambda: run(ctx, ['traffic', 'resume', '--job', ask('完整任务编号')])),
               ('12', '添加授权下载资源', lambda: resource(ctx)),
               ('13', '移除授权下载资源', lambda: resource(ctx, True)),
               ('14', '实时下载进度（每秒刷新）', lambda: watch_progress(ctx)),
               ('15', '切换下载模式（固定额度 / 累计比例）', lambda: configure_strategy(ctx)),
               ('16', '多连接 / 仅下行测速 / 实时限速', lambda: configure_download(ctx)),
               ('17', '每批下载大小 / 任务随机间隔', lambda: configure_batches(ctx))]
    submenu(ctx, '下载任务调度', entries)


def update(ctx):
    if run(ctx, ['update', '--check']):
        return
    if ask('下载、校验并更新管理工具？y/N', 'N').lower() == 'y':
        run(ctx, ['update'])
    else:
        print('未执行更新。')


def watch_progress(ctx):
    from .progress import watch
    return watch(ctx)


def menu(ctx):
    labels = ['查看系统信息', '查看云平台信息', '检查客户机 Agent', '管理可选 Agent 官方卸载', '验证卸载结果',
              '安装/修复 PF', 'Realm 转发规则管理', 'Realm 服务管理', '手动下行下载测试', '下载任务调度',
              '流量统计', '网络诊断', 'SSH 与网络检查', '配置备份', '配置恢复', '查看日志', '更新管理工具', '卸载管理工具']
    commands = {'1': ['info'], '2': ['cloud', 'show'], '3': ['agent', 'audit'], '5': ['agent', 'verify'],
                '6': ['pf', 'repair'], '11': ['traffic', 'status'], '12': ['network'], '13': ['health'],
                '14': ['backup', 'create'], '16': ['logs'], '18': ['uninstall']}
    try:
        while True:
            choice = choose('主菜单', [(str(i + 1), label) for i, label in enumerate(labels)], '退出工具', {'19': '0'})
            if choice is None:
                print('\n已退出 VPS Toolkit。')
                return
            title = labels[int(choice) - 1]
            if choice == '7':
                rule_menu(ctx)
            elif choice == '8':
                service_menu(ctx)
            elif choice == '10':
                traffic_menu(ctx)
            elif choice == '4':
                agent_menu(ctx)
            elif choice in ('3', '5'):
                agent_audit(ctx)
            elif choice == '11':
                action_page(ctx, '实时下载进度', lambda: watch_progress(ctx))
            elif choice == '9':
                action_page(ctx, title, lambda: download(ctx))
            elif choice == '15':
                action_page(ctx, title, lambda: run(ctx, ['backup', 'restore', ask('备份文件路径'), '--apply']))
            elif choice == '17':
                action_page(ctx, title, lambda: update(ctx))
            else:
                command_page(ctx, title, commands[choice])
    except (EndMenu, KeyboardInterrupt):
        print('\n已退出 VPS Toolkit。')
