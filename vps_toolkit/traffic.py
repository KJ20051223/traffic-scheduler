import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid
from urllib.parse import urlsplit
from compat import traffic_scheduler as engine
from .core import Context, Error, configuration, read_json, write_json
from . import ratio, progress, __version__


def moment(timezone='system', timestamp=None):
    timestamp = time.time() if timestamp is None else timestamp
    if timezone == 'system':
        return dt.datetime.fromtimestamp(timestamp).astimezone()
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.fromtimestamp(timestamp, ZoneInfo(timezone))
    except ImportError:
        pass
    except Exception as exc:
        raise Error('时区不可用: ' + str(exc))
    if not re.fullmatch(r'[A-Za-z0-9_+./-]+', timezone) or '..' in timezone.split('/'):
        raise Error('时区无效')
    if not Path('/usr/share/zoneinfo', timezone).is_file() or not hasattr(time, 'tzset'):
        raise Error('需要安装 tzdata 才能使用该时区')
    previous = os.environ.get('TZ')
    try:
        os.environ['TZ'] = timezone
        time.tzset()
        return dt.datetime.fromtimestamp(timestamp).astimezone()
    finally:
        if previous is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous
        time.tzset()


def validate(item):
    ratio.validate(item)
    for field in ('daily_bytes', 'monthly_bytes', 'task_bytes', 'max_runtime', 'max_concurrency'):
        if type(item.get(field)) is not int or item[field] <= 0:
            raise Error(field + ' 必须是正整数')
    if item['max_concurrency'] > 32:
        raise Error('最大并发上限为 32；当前共享计量实现始终串行，实际并发不超过 1')
    for field in ('mbps', 'reserve_mbps'):
        value = item.get(field)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise Error('速度和预留带宽必须是有限非负数')
    if not 0.01 <= item['mbps'] <= 10000:
        raise Error('速度须在 0.01–10000 Mbps 之间')
    if item.get('link_mbps') is not None:
        value = item['link_mbps']
        if type(value) not in (int, float) or not math.isfinite(value) or value <= item['reserve_mbps']:
            raise Error('链路带宽必须大于 Realm 预留带宽')
    elif item['reserve_mbps']:
        raise Error('配置预留带宽时必须同时提供链路总带宽')
    weights = item.get('hourly_weights', [1] * 24)
    if len(weights) != 24 or any(type(w) not in (float, int) or not math.isfinite(w) or not 0 < w <= 1e6 for w in weights):
        raise Error('小时权重须为 24 个正数')
    for value in list(item.get('times', [])) + item.get('window', []):
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
            raise Error('任务时间必须是 HH:MM')
    if len(item.get('window', [])) != 2:
        raise Error('需提供起止时间窗口')
    moment(item.get('timezone', 'system'))
    if not isinstance(item.get('authorized_urls'), list):
        raise Error('authorized_urls 必须是列表')
    for url in item['authorized_urls']:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise Error('测试 URL 必须是无凭据和片段的 HTTP(S) 地址')
    if item.get('region') not in ('cn', 'overseas'):
        raise Error('下载地区须为 cn 或 overseas')
    return item


def effective_rate(item):
    rate = item['mbps']
    if item.get('link_mbps') is not None:
        rate = min(rate, item['link_mbps'] - item['reserve_mbps'])
    return rate


def ledger(ctx, item, at=None):
    at = at or moment(item['timezone'])
    directory = ctx.path(item['state_dir'])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ('quota-' + at.date().isoformat() + '.json')
    data = engine.read_day(path, at.date().isoformat())
    engine.recover(data)
    return directory, path, data, at


def remaining(item, directory, data):
    month = data['date'][:7]
    total = data['download']['bytes'] + data['download']['reserved']
    for path in Path(directory).glob('quota-' + month + '-??.json'):
        if path.name == 'quota-' + data['date'] + '.json':
            continue
        record = engine.read_day(path, path.stem[6:])
        total += record['download']['bytes'] + record['download']['reserved']
    return max(0, min(item['daily_bytes'] - data['download']['bytes'], item['monthly_bytes'] - total))


def legacy_allowance(config, data, directory):
    """Called by the compatible legacy tick while holding the SAME quota lock."""
    item = configuration(Context())['traffic']
    validate(item)
    if Path(item['state_dir']).resolve() != Path(directory).resolve():
        raise Error('旧任务与统一预算使用不同统计目录，拒绝绕过预算')
    if item['paused']:
        return 0
    # Apply the global ceiling to compatible legacy sessions too, after their
    # original test fingerprint has been checked. Never raise the old rate.
    config['directions']['download']['mbps'] = min(config['directions']['download']['mbps'], effective_rate(item))
    if any(target['url'] not in item['authorized_urls'] for target in engine.targets(config, 'download')):
        raise Error('原任务包含未授权的 URL，请先 traffic authorize')
    return min(remaining(item, directory, data), ratio.allowance(Context(), item))


def legacy_realtime():
    item = validate(configuration(Context())['traffic'])
    return item['strategy'] == 'ratio' and item['ratio_schedule'] == 'follow'


def import_legacy(ctx):
    config = configuration(ctx)
    path = ctx.path('/etc/traffic-scheduler/config.json')
    if path.exists() and not config.get('legacy_imported'):
        old = engine.load_config(path)
        item = old['directions']['download']
        config['traffic'].update(daily_bytes=item['daily_bytes'], mbps=item['mbps'], region=item['region'],
                                 timezone=old['timezone'], hourly_weights=old.get('hourly_weights', [1] * 24),
                                 authorized_urls=[x['url'] for x in engine.targets(old, 'download')])
        config['legacy_imported'] = True
    return config


def legacy_test(config, directory, megabytes):
    context = Context()
    settings = configuration(context)['traffic']
    if Path(settings['state_dir']).resolve() != Path(directory).resolve():
        raise Error('旧测试目录与统一预算不一致')
    url = engine.targets(config, 'download')[0]['url']
    result = download(context, size=int(megabytes * 1e6), url=url, ignore_ratio=True)
    if result['status'] != 'completed':
        raise Error('手动测试未完整完成，当前配置未获得启用许可')
    engine.atomic_json(Path(directory) / 'last-test.json', {
        'at': moment(settings['timezone']).isoformat(), 'version': 'vps-toolkit-' + __version__,
        'download': {'bytes': result['received_bytes'], 'confirmed': result['received_bytes'], 'reserved': 0},
        'config_hash': hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()})


def task_directory(ctx):
    return ctx.state / 'traffic'


def initialize_meter(ctx, item=None):
    """Automatically establish a missing baseline, without waiting for a busy transfer."""
    item = validate(item if item is not None else configuration(ctx)['traffic'])
    if item['strategy'] != 'ratio':
        return {'status': 'not_applicable', 'note': '当前为固定额度模式'}
    path = ratio.state_path(ctx)
    try:
        state = read_json(path)
        if state is not None:
            ratio.check_state(state)
            if state['selector'] != item['ratio_interface']:
                raise Error('已有累计计量网卡与设置不一致；请通过累计流量校准切换，不自动清零')
            return {'status': 'ready', 'note': '已有整机累计计量记录，继续累计，不重置历史'}
        selected = ratio.auto_interfaces(ctx) if item['ratio_interface'] == 'auto' else ratio.names(item['ratio_interface'])
        ratio.snapshot(ctx, selected)  # Diagnose unreadable counters before attempting the shared lock.
        with engine.locked(ctx.path(item['state_dir'])):
            machine = ratio.sample(ctx, item)
        return {'status': 'ready', 'note': '已自动读取本次开机网卡累计流量并保存计量基线，无需手动输入',
                'machine_traffic': machine}
    except engine.Stop:
        return {'status': 'pending', 'note': '下载正在使用计量锁；进度页先显示网卡实测预览，空闲后自动保存基线'}
    except (Error, OSError) as exc:
        return {'status': 'unavailable', 'note': str(exc),
                'manual_interface': not path.exists(),
                'help': '请在比例设置中手动指定真实出口网卡；重启前丢失的历史可在累计流量校准中补录。实时计数不可读时停止比例补下载，不按零流量计算。'}


def status(ctx, at=None):
    item = validate(configuration(ctx)['traffic'])
    with engine.locked(ctx.path(item['state_dir'])):
        directory, path, data, at = ledger(ctx, item, at)
        month_remaining = remaining(dict(item, daily_bytes=10**30), directory, data)
        machine = ratio.sample(ctx, item) if item['strategy'] == 'ratio' else None
        if machine is not None:
            machine['fillable_now_bytes'] = min(machine['needed_bytes'], remaining(item, directory, data))
            machine['limited_by_budget'] = machine['needed_bytes'] > machine['fillable_now_bytes']
    jobs = [read_json(p) for p in sorted((task_directory(ctx) / 'jobs').glob('*.json'))[-30:]]
    return dict(time=at.isoformat(), settings=item, today_bytes=data['download']['bytes'],
                monthly_remaining=month_remaining, remaining_bytes=remaining(item, directory, data), jobs=jobs,
                machine_traffic=machine,
                legacy_timer_preserved=ctx.path('/etc/systemd/system/traffic-scheduler.timer').exists(),
                note='仅统计有效负载；协议开销另计。所有工具下载共用计量锁；实际并发为 1。')


def download(ctx, size=None, mbps=None, url=None, max_runtime=None, expected_sha256=None,
             network=None, at=None, job_id=None, ignore_ratio=False):
    item = validate(configuration(ctx)['traffic'])
    if item['paused']:
        raise Error('下载已暂停，请先 traffic resume')
    urls = item['authorized_urls']
    url = url or (urls[0] if urls else None)
    if not url or url not in urls:
        raise Error('先使用 traffic authorize URL 明确授权 HTTP(S) 测试资源')
    if expected_sha256 and not re.fullmatch(r'[0-9a-fA-F]{64}', expected_sha256):
        raise Error('完整性 SHA256 无效')
    amount = min(10_000_000, item['task_bytes']) if size is None else size
    if type(amount) is not int or not 0 < amount <= item['task_bytes']:
        raise Error('下载量须大于 0 且不超过每任务预算')
    rate = effective_rate(item) if mbps is None else min(mbps, effective_rate(item))
    runtime = item['max_runtime'] if max_runtime is None else min(max_runtime, item['max_runtime'])
    if type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0 or runtime <= 0:
        raise Error('任务限速和运行时间必须为正数')
    directory = ctx.path(item['state_dir'])
    jobs = task_directory(ctx) / 'jobs'
    jobs.mkdir(parents=True, exist_ok=True)
    identifier = job_id or uuid.uuid4().hex
    if not re.fullmatch(r'[0-9a-f]{32}', identifier):
        raise Error('任务编号无效')
    job_path = jobs / (identifier + '.json')
    job = read_json(job_path) if job_id else None
    if job:
        if job['status'] not in ('paused', 'stopped', 'interrupted') or expected_sha256 or job.get('sha256'):
            raise Error('该任务不可恢复；要求完整 SHA256 的任务须从头重新校验')
        amount, url = job['requested_bytes'], job['url']
        if amount > item['task_bytes']:
            raise Error('原任务超过当前每任务预算，拒绝恢复')
        if url not in urls:
            raise Error('原任务 URL 已撤销授权')
        offset = job['received_bytes']
    else:
        offset = 0
        job = dict(id=identifier, url=url, requested_bytes=amount, received_bytes=0, status='running',
                   started=moment(item['timezone']).isoformat(), sha256=expected_sha256, ignore_ratio=ignore_ratio)
    hasher = hashlib.sha256() if expected_sha256 else None
    with engine.locked(directory):
        directory, path, data, current = ledger(ctx, item, at)
        if remaining(item, directory, data) <= 0:
            raise Error('每日或每月流量预算已用完')
        write_json(task_directory(ctx) / 'active.json', dict(id=identifier, pid=os.getpid()))
        write_json(job_path, dict(job, status='running'))
        network = network or engine.Network(engine.load_geo(directory))
        pacer = engine.Pacer(rate)
        retries = 0
        started = time.monotonic()
        if at is None:
            next_day = current.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)
            runtime = min(runtime, max(0.001, next_day.timestamp() - time.time()))
        try:
            with engine.deadline(runtime):
                while offset < amount:
                    flags = read_json(task_directory(ctx) / 'control.json', {})
                    if flags.get('stop') == identifier or flags.get('pause'):
                        job['status'] = 'paused' if flags.get('pause') else 'stopped'
                        break
                    # Stop at a calendar boundary; next invocation uses the new ledger.
                    if at is None and moment(item['timezone']).date() != current.date():
                        job['status'] = 'interrupted'
                        break
                    budget = remaining(item, directory, data)
                    if budget <= 0:
                        job['status'] = 'budget_exhausted'
                        break
                    if not ignore_ratio:
                        budget = min(budget, ratio.allowance(ctx, item))
                        if budget <= 0:
                            job['status'] = 'ratio_satisfied'
                            break
                    piece = min(engine.PIECE, amount - offset, budget, max(1, int(rate * 125000 * 8)))
                    before = data['download']['bytes']
                    try:
                        kind = 'generated' if urlsplit(url).hostname == 'speed.cloudflare.com' and urlsplit(url).path == '/__down' else 'file'
                        engine.transfer(network, 'download', {'region': item['region'], 'mbps': rate},
                                        {'name': '用户授权资源', 'url': url, 'kind': kind}, piece,
                                        directory, data, path, pacer, offset=offset, hasher=hasher,
                                        progress=progress.reporter(directory, data, requested=amount, job_id=identifier, offset=offset))
                        retries = 0
                    except engine.ERRORS as exc:
                        retries += 1
                        if retries >= 3 or not engine.transient(exc):
                            raise
                        time.sleep(retries)
                    finally:
                        offset += data['download']['bytes'] - before
                        job['received_bytes'] = offset
                        write_json(job_path, job)
                    if time.monotonic() - started >= runtime:
                        raise engine.Stop('任务运行时间已到')
                if offset >= amount:
                    if hasher is not None and hasher.hexdigest() != expected_sha256.lower():
                        raise Error('下载 SHA256 不匹配，已计入实际流量；临时内容已清理')
                    job['status'] = 'completed'
                    job['actual_sha256'] = hasher.hexdigest() if hasher else None
        except engine.Stop:
            job['status'] = 'interrupted'
        except BaseException:
            job['status'] = 'failed'
            raise
        finally:
            job['finished'] = moment(item['timezone']).isoformat()
            write_json(job_path, job)
            progress.finish(directory, job['status'])
            active = task_directory(ctx) / 'active.json'
            if active.exists():
                active.unlink()
            engine.atomic_json(directory / 'geo.json', network.cache if hasattr(network, 'cache') else {})
            if item['strategy'] == 'ratio':
                ratio.sample(ctx, item)
        return job


def set_control(ctx, action):
    config = configuration(ctx)
    if action != 'stop':
        config['traffic']['paused'] = action == 'pause'
    from .core import save_config
    save_config(ctx, config)
    directory = task_directory(ctx)
    directory.mkdir(parents=True, exist_ok=True)
    active = read_json(directory / 'active.json', {})
    write_json(directory / 'control.json', {'pause': config['traffic']['paused'],
                                           'stop': active.get('id') if action == 'stop' else None})


def tick(ctx, at=None):
    item = validate(configuration(ctx)['traffic'])
    at = at or moment(item['timezone'])
    directory = ctx.path(item['state_dir'])
    with engine.locked(directory):
        machine = ratio.sample(ctx, item) if item['strategy'] == 'ratio' else None
        if not item['enabled'] or item['paused']:
            return {'skipped': True}
        directory, path, data, _ = ledger(ctx, item, at)
        minute = at.hour * 60 + at.minute
        config_hash = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
        plan_path = task_directory(ctx) / ('plan-' + at.date().isoformat() + '.json')
        plan = read_json(plan_path)
        room = remaining(item, directory, data)
        if not room:
            return {'skipped': True, 'reason': 'budget'}
        if machine is not None:
            room = min(room, machine['needed_bytes'])
            if not room:
                return {'skipped': True, 'reason': 'ratio_satisfied', 'machine_traffic': machine}
        follow = machine is not None and item['ratio_schedule'] == 'follow'
        if follow:
            start, end = [int(v[:2]) * 60 + int(v[3:]) for v in item['window']]
            inside = start <= minute <= end if start <= end else minute >= start or minute <= end
            fixed = {int(t[:2]) * 60 + int(t[3:]) for t in item['times']}
            plan = read_json(plan_path, {})
            if not inside or (fixed and minute not in fixed) or plan.get('last_follow') == minute:
                return {'skipped': True, 'reason': 'window_or_already_checked'}
            size = min(room, item['task_bytes'], max(1, int(effective_rate(item) * 125000 * 40)))
            write_json(plan_path, {'last_follow': minute})
        else:
            size = plan_random(ctx, item, at, directory, data, room, plan_path, plan, config_hash)
            if size is None:
                return {'skipped': True}
    # Release planning lock; transfer rechecks both shared budgets and actual NIC traffic.
    return download(ctx, size=size, max_runtime=min(48, item['max_runtime']), at=at)


def plan_random(ctx, item, at, directory, data, room, plan_path, plan, config_hash):
    minute = at.hour * 60 + at.minute
    legacy = engine.default_config()
    legacy['directions']['download'].update(daily_bytes=data['download']['bytes'] + room, mbps=effective_rate(item))
    legacy['hourly_weights'] = item['hourly_weights']
    if not plan or plan.get('settings_hash') != config_hash:
        data['plan'] = None
        generated = engine.make_plan(legacy, data, at)
        start, end = [int(v[:2]) * 60 + int(v[3:]) for v in item['window']]
        candidates = [n for n in range(max(minute, generated['not_before']), 1440)
                      if (start <= n <= end if start <= end else n >= start or n <= end)]
        needed = math.ceil(room / min(item['task_bytes'], effective_rate(item) * 125000 * 40))
        candidates = sorted(engine.weighted_minutes(legacy, candidates, min(len(candidates), needed), engine.random.SystemRandom()))
        if item['times']:
            candidates = sorted({int(t[:2]) * 60 + int(t[3:]) for t in item['times']
                                 if int(t[:2]) * 60 + int(t[3:]) >= minute})
        plan = {'settings_hash': config_hash, 'minutes': candidates, 'last_tick': None,
                'not_before': generated['not_before'] if not item['times'] else 0}
        write_json(plan_path, plan)
    elif not item['times']:
        start, end = [int(v[:2]) * 60 + int(v[3:]) for v in item['window']]
        available = {n for n in range(max(minute, plan['not_before']), 1440)
                     if (start <= n <= end if start <= end else n >= start or n <= end)}
        planned = set(plan['minutes'])
        needed = min(len(available), math.ceil(room / min(item['task_bytes'], effective_rate(item) * 125000 * 40)))
        extra = needed - len(planned & available)
        if extra > 0:
            planned.update(engine.weighted_minutes(legacy, sorted(available - planned), extra, engine.random.SystemRandom()))
            plan['minutes'] = sorted(planned)
            write_json(plan_path, plan)
    if minute not in plan['minutes'] or plan['last_tick'] == minute:
        return None
    future = sum(n >= minute for n in plan['minutes'])
    size = min(item['task_bytes'], math.ceil(room / max(1, future)))
    plan['last_tick'] = minute
    write_json(plan_path, plan)
    return size
