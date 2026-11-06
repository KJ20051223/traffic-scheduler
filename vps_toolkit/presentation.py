"""Readable terminal reports; JSON remains available to command-line automation."""
import datetime as dt
import json
import re
from . import __version__


CLOUDS = {'aliyun': '阿里云', 'tencent': '腾讯云', 'huawei': '华为云', 'baidu': '百度云',
          'ucloud': 'UCloud', 'jdcloud': '京东云', 'generic': '通用服务器', 'unknown': '未能识别'}
AGENTS = {'aliyun-assist': '阿里云助手', 'aegis': '阿里云安全中心', 'cloudmonitor': '阿里云监控',
          'tat': '腾讯云自动化助手 TAT', 'barad': '腾讯云监控 Barad', 'sgagent': '腾讯云 Stargate',
          'yunjing': '腾讯云主机安全', 'cloud-init': 'cloud-init 初始化组件'}
STATES = {'completed': '已完成', 'running': '运行中', 'failed': '失败', 'paused': '已暂停',
          'stopped': '已停止', 'interrupted': '已中断', 'budget_exhausted': '预算已用完',
          'ratio_satisfied': '比例已满足，无需继续下载', 'quota': '固定额度', 'ratio': '累计比例补下行',
          'follow': '按分钟跟随', 'random': '全天随机分散', 'cn': '中国大陆', 'overseas': '海外',
          'pause': '暂停下载', 'resume': '解除暂停', 'stop': '停止当前下载'}
STATES.update({'ready': '可执行', 'blocked': '未满足执行条件', 'excluded': '未选择', 'not_detected': '未确认安装',
               'removed': '已卸载', 'residual': '仍有残留/需复核', 'disabled_only': '仅禁用下次启动，未完全卸载',
               'idle': '等待下次任务', 'connecting': '正在连接', 'receiving': '正在下载',
               'between_requests': '分块已结算，准备下一块', 'not_run': '未执行'})
STATES['cooldown'] = '任务间隔：随机等待'
LABELS = {'saved': '已保存至', 'exported': '已导出至', 'archive': '备份文件', 'files': '文件数量/列表',
          'output': '执行输出', 'returncode': '退出状态', 'note': '说明', 'reason': '原因', 'action': '操作',
          'version': '版本', 'installed': '当前版本', 'available': '可用版本', 'sha256': 'SHA256',
          'component': '组件', 'official_document': '官方文档', 'impact': '操作影响', 'at': '时间', 'operation': '操作',
          'console_precondition': '需要先在控制台授权', 'command': '官方执行命令', 'skipped': '已跳过',
          'verified': '复核结果', 'reinstall': '重装参考', 'simulation': '卸载模拟结果',
          'simulation_returncode': '模拟退出状态', 'persistent_network_files': '网络持久化配置',
          'requires_second_confirmation': '需要独立确认', 'preserved_network_files': '已保留网络配置',
          'route_unchanged': '默认路由未改变', 'applied': '已应用', 'uninstalled': '已卸载',
          'preserved': '已保留', 'binary': '程序路径', 'config': '配置文件', 'error': '错误',
          'config_error': '配置错误', 'service': '服务状态', 'logs': '最近日志', 'listeners': '监听端口',
          'architecture': 'CPU 架构', 'package_manager': '包管理器', 'init': '服务管理器',
          'libc': 'C 运行库', 'libc_version': '运行库版本', 'warnings': '提示', 'id': '编号',
          'state': '状态', 'paths': '发现路径', 'processes': '相关进程', 'services': '相关服务',
          'packages': '相关软件包', 'protection': '自保护状态', 'startup_or_install_records': '启动/安装记录',
          'existing_daily_counts_preserved': '已有用量已保留', 'acceptance': '最终检查', 'backup': '备份',
          'rules': '转发规则', 'elf': '程序兼容信息', 'machine_traffic': '整机累计流量',
          'needed_bytes': '待补下行', 'upload_bytes': '累计上行', 'download_bytes': '累计下行',
          'target_download_bytes': '目标下行', 'deficit_bytes': '原始缺口', 'target_ratio': '目标上行:下行',
          'actual_down_per_up': '当前下行/上行', 'interfaces': '计量网卡', 'calibrated': '已人工校准',
          'started': '开始时间', 'sampled_at': '采样时间', 'fillable_now_bytes': '剩余预算内可补',
          'limited_by_budget': '缺口受预算限制', 'readable': '可读取', 'ok': '正常',
          'baseline_persisted': '累计计量基线已保存',
          'confidence': '识别依据强度', 'evidence': '识别依据', 'instance_type': '实例类型',
          'remote': '目标地址', 'listen': '监听地址', 'network': '网络设置', 'endpoints': '转发规则',
          'no_tcp': '关闭 TCP', 'use_udp': '启用 UDP', 'extra_remotes': '额外后端', 'balance': '负载策略',
          'enabled': '新调度入口已启用', 'paused': '下载已暂停', 'timezone': '时区', 'mbps': '限速 Mbps',
          'daily_bytes': '每日补下载上限', 'monthly_bytes': '每月补下载上限', 'task_bytes': '单任务上限',
          'max_runtime': '单任务最长秒数', 'max_concurrency': '并发上限', 'link_mbps': '链路带宽 Mbps',
          'reserve_mbps': '业务预留 Mbps', 'authorized_urls': '已授权下载地址', 'region': '目标地区',
          'state_dir': '统计目录', 'times': '固定时刻', 'window': '允许时段', 'hourly_weights': '小时权重',
          'strategy': '下载策略', 'ratio': '目标上行:下行', 'ratio_interface': '计量网卡设置',
          'ratio_tolerance_pct': '比例容差 %', 'ratio_schedule': '补流调度', 'legacy_timer_preserved': '检测到旧定时配置'}


def clean(value):
    text = str(value)
    text = re.sub(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))', '', text)
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)


def scalar(value):
    if value is None:
        return '未提供'
    if type(value) is bool:
        return '是' if value else '否'
    return clean(STATES.get(value, value) if isinstance(value, str) else value)


def size(value):
    value = int(value)
    return '{:.3f} GB'.format(value / 1e9) if abs(value) >= 1e9 else '{:.2f} MB'.format(value / 1e6)


def row(label, value):
    print('  {}：{}'.format(label, scalar(value)))


def section(title, text=None):
    print('\n【{}】'.format(title))
    if text is not None:
        for line in clean(text).splitlines() or ['暂无内容']:
            print('  ' + line)


def tree(value, indent=2):
    prefix = ' ' * indent
    if isinstance(value, dict):
        if not value:
            print(prefix + '暂无内容')
        for key, entry in value.items():
            label = LABELS.get(key, key)
            if isinstance(entry, (dict, list)) and entry:
                print(prefix + clean(label) + '：')
                tree(entry, indent + 2)
            else:
                shown = size(entry) if key.endswith('_bytes') and type(entry) is int else scalar(entry) if not isinstance(entry, (dict, list)) else '暂无'
                if key in ('started', 'sampled_at') and type(entry) in (int, float):
                    shown = dt.datetime.fromtimestamp(entry).astimezone().isoformat(timespec='seconds')
                print(prefix + clean(label) + '：' + shown)
    elif isinstance(value, list):
        for entry in value:
            if isinstance(entry, (list, dict)):
                print(prefix + '—')
                tree(entry, indent + 2)
            else:
                print(prefix + '• ' + scalar(entry))
    else:
        for line in scalar(value).splitlines():
            print(prefix + line)


def cloud(value):
    row('云平台', CLOUDS.get(value.get('provider'), value.get('provider', 'unknown')))
    for key in ('confidence', 'instance_type'):
        row(LABELS[key], value.get(key))
    row('识别依据', '；'.join(value.get('evidence', [])))


def health(value, module):
    platform = value.get('platform', {})
    release = platform.get('os', {})
    section('系统概况')
    row('操作系统', release.get('PRETTY_NAME') or '{} {}'.format(release.get('ID', '未知'), release.get('VERSION_ID', '')))
    for key in ('architecture', 'package_manager', 'init'):
        row(LABELS[key], platform.get(key))
    row('运行库', '{} {}'.format(platform.get('libc', ''), platform.get('libc_version', '')))
    cloud(value.get('cloud', {}))
    for warning in platform.get('warnings', []):
        row('提示', warning)
    titles = {'interfaces': '网卡地址', 'routes': '默认路由', 'listeners': '监听端口', 'ssh': '连接与 SSH',
              'virtualization': '虚拟化环境', 'memory': '内存（MB）', 'disk': '磁盘空间'}
    keys = ('memory', 'disk', 'virtualization') if module == 'info' else tuple(titles)
    for key in keys:
        result = value.get(key, {})
        section(titles[key] + (' · 正常' if result.get('ok') else ' · 未能读取'), result.get('details', '').strip() or '暂无输出')
    if module != 'info':
        section('DNS', '\n'.join(value.get('dns', [])) or '未读取到 DNS 配置')
    if value.get('existing'):
        section('已有组件')
        labels = {'pf': 'PF 入口', 'realm': 'Realm 程序', 'realm_config': 'Realm 配置',
                  'traffic_scheduler': '兼容下载入口', 'traffic_config': '原下载配置', 'backup': '备份目录'}
        for key, exists in value['existing'].items():
            row(labels.get(key, key), '已发现' if exists else '未发现')


def agents(value):
    cloud(value.get('cloud', {}))
    section('Agent 检测结果')
    for component in value.get('components', []):
        row(AGENTS.get(component['id'], component['id']), component['state'])
    for component in value.get('components', []):
        evidence = {key: component[key] for key in ('paths', 'processes', 'services', 'packages', 'startup_or_install_records') if component.get(key)}
        if evidence:
            section(AGENTS.get(component['id'], component['id']) + ' · 发现依据')
            tree(evidence)
            row('官方文档', component.get('official_document'))
    for message in value.get('limitations', []):
        row('检查限制', message)
    if value.get('layers'):
        section('检测范围', '\n'.join(value['layers'].values()))
    section('控制台核对', value.get('console_notice', ''))


def traffic_settings(item):
    for key in ('strategy', 'enabled', 'paused', 'daily_bytes', 'monthly_bytes', 'task_bytes', 'mbps', 'region', 'timezone'):
        if key in item:
            row(LABELS[key], size(item[key]) if key.endswith('_bytes') else item[key])
    row('下载方式', '仅下行测速' if item.get('download_mode') == 'speedtest' else '文件分段下载')
    row('最大连接数', item['max_concurrency'])
    row('每批目标时间', '{} 秒'.format(item.get('batch_seconds', 8)))
    row('批次随机间隔', '{}–{} 秒'.format(item.get('cooldown_min_seconds', 5), item.get('cooldown_max_seconds', 10)))
    if item.get('strategy') == 'ratio':
        for key in ('ratio', 'ratio_schedule', 'ratio_interface', 'ratio_tolerance_pct'):
            row(LABELS[key], item.get(key))
    row('允许时段', ' — '.join(item.get('window', [])))
    row('固定时刻', '、'.join(item.get('times', [])) or '未指定')
    row('已授权下载资源', '{} 个'.format(len(item.get('authorized_urls', []))))


def traffic_status(value):
    row('统计时间', value['time'])
    section('下载设置')
    traffic_settings(value['settings'])
    section('本工具下载用量')
    row('今日已用', size(value['today_bytes']))
    row('本月剩余额度', size(value['monthly_remaining']))
    row('当前可用额度', size(value['remaining_bytes']))
    if value.get('machine_traffic'):
        section('整机累计比例')
        tree(value['machine_traffic'])
    jobs = value.get('jobs', [])[-5:]
    if jobs:
        section('最近下载任务')
        for job in jobs:
            row(job.get('id', '')[:12], '{} · {} / {}'.format(scalar(job.get('status')), size(job.get('received_bytes', 0)), size(job.get('requested_bytes', 0))))
    row('说明', value.get('note', ''))
    if value.get('legacy_timer_preserved'):
        row('旧入口', '检测到兼容定时配置，其启用状态由服务管理器决定')


def rules(value):
    endpoints = value.get('endpoints', [])
    if not endpoints:
        print('  暂无转发规则。')
    for index, endpoint in enumerate(endpoints):
        network = endpoint.get('network', value.get('network', {}))
        protocols = 'UDP' if network.get('no_tcp') else 'TCP + UDP' if network.get('use_udp') else 'TCP'
        row('规则 {}'.format(index), '{} → {}  [{}]'.format(endpoint.get('listen'), endpoint.get('remote'), protocols))
        extras = {k: v for k, v in endpoint.items() if k not in ('listen', 'remote', 'network')}
        if extras:
            tree(extras, 4)


def render_progress(value):
    row('下载状态', value['status'])
    row('入口', value['entry'])
    if value.get('id'):
        row('任务编号', value['id'])
    if value.get('requested_bytes'):
        percent = value.get('percent') or 0
        filled = int(percent * 24 / 100)
        row('当前任务进度', '[{}{}] {:.1f}%'.format('#' * filled, '-' * (24 - filled), percent))
        row('已接收 / 本次目标', '{} / {}'.format(size(value['received_bytes']), size(value['requested_bytes'])))
        row('本次任务还剩', size(max(0, value['requested_bytes'] - value['received_bytes'])))
    row('当前有效负载速度', '{:.2f} Mbps'.format(value['speed_mbps']))
    row('设置限速', '{} Mbps'.format(value['configured_mbps']))
    if value.get('cooldown_remaining_seconds', 0) > 0:
        row('下批开始前还需等待', '{:.1f} 秒'.format(value['cooldown_remaining_seconds']))
    if 'connections' in value:
        row('连接数 / 配置上限', '{} / {}'.format(value['connections'], value['max_concurrency']))
        row('下载方式', '仅下行测速' if value['download_mode'] == 'speedtest' else '文件分段下载')
    if value.get('eta_seconds') is not None:
        row('本次任务估计剩余', '{:.0f} 秒'.format(value['eta_seconds']))
    section('今日下载')
    row('今日已接收（含待结算）', size(value['today_bytes']))
    row('今日已结算', size(value['today_settled_bytes']))
    row('每日补下载上限', size(value['daily_limit_bytes']))
    row('当前剩余下载预算', size(value['remaining_bytes']))
    row('策略', value['strategy'])
    row('下载已暂停', value['paused'])
    if value.get('machine_traffic'):
        section('整机累计比例')
        machine = value['machine_traffic']
        if 'needed_bytes' in machine:
            row('达到比例还需补下载（含容差）', size(machine['needed_bytes']))
            row('当前预算内最多可补', size(machine['fillable_now_bytes']))
        tree({key: data for key, data in machine.items() if key not in ('needed_bytes', 'fillable_now_bytes')})
    row('说明', value['note'])


def render_batch(value):
    if value.get('batch_plan'):
        row('本次范围', '全部云 Agent（含 cloud-init）' if value['scope'] == 'all' and value['include_cloud_init'] else '本次指定组件')
        row('影响', value['impact'])
        for item in value['entries']:
            if item['status'] == 'not_detected':
                continue
            section(AGENTS.get(item['id'], item['id']) + ' · ' + scalar(item['status']))
            if item['reason']:
                row('说明', item['reason'])
            if item.get('fallback_disable'):
                row('卸载失败备选', '使用 cloud-init 官方标记禁用下次启动；会明确报告仅禁用')
            for step in item['steps']:
                row('将执行', ' '.join(step['argv']) if step.get('argv') else
                    '清理云助手官方指定目录' if step['kind'] == 'cleanup_assist' else '创建 cloud-init 官方禁用标记')
                if step.get('sha256'):
                    row('确认后变更校验 SHA256', step['sha256'])
            row('官方文档', item['official_document'])
        row('说明', value['note'])
    else:
        for item in value['results']:
            if item['status'] == 'not_detected':
                continue
            section(AGENTS.get(item['id'], item['id']) + ' · ' + scalar(item['status']))
            if item.get('reason'):
                row('说明', item['reason'])
            if item.get('output'):
                section('官方程序输出', item['output'])
        row('完整报告', value['report_file'])
        row('总体结果', '已完成本次可确认的卸载' if value['returncode'] == 0 else '部分项目未完成，请查看上方原因')


def render(value, args=None):
    if value is None:
        return
    if not isinstance(value, dict):
        tree(value)
        return
    module = getattr(args, 'module', None)
    if value.get('progress_view'):
        render_progress(value)
    elif value.get('batch_plan') or value.get('batch_result'):
        render_batch(value)
    elif 'platform' in value and 'cloud' in value:
        health(value, module)
    elif 'provider' in value:
        cloud(value)
    elif 'components' in value:
        agents(value)
    elif 'settings' in value and 'today_bytes' in value:
        traffic_status(value)
    elif 'traffic' in value:
        row('配置', '已保存，已有流量记录保留')
        row('文件', value.get('saved'))
        section('当前下载设置')
        traffic_settings(value['traffic'])
        if value.get('machine_initialization'):
            initialization = value['machine_initialization']
            section('整机流量自动读取', initialization['note'])
            if initialization.get('machine_traffic'):
                tree(initialization['machine_traffic'])
            if initialization.get('help'):
                row('处理方式', initialization['help'])
    elif 'requested_bytes' in value and 'received_bytes' in value:
        row('下载结果', value.get('status'))
        row('任务编号', value.get('id'))
        row('实际接收', size(value['received_bytes']))
        row('本次请求', size(value['requested_bytes']))
        row('临时内容', '已自动清理')
        if value.get('actual_sha256'):
            row('SHA256', value['actual_sha256'])
    elif 'endpoints' in value:
        rules(value)
    elif 'binary' in value and 'rules' in value:
        tree({k: v for k, v in value.items() if k != 'rules'})
        section('转发规则')
        rules(value['rules'])
    elif 'events' in value:
        if not value['events']:
            print('  暂无操作日志。')
        for event in value['events']:
            try:
                event = json.loads(event)
            except (ValueError, TypeError):
                pass
            tree(event)
            print()
    elif set(value) == {'returncode'}:
        # The low-privilege child already rendered its own result.
        if value['returncode']:
            row('执行结果', '未完成，请查看上方错误')
    else:
        tree(value)
