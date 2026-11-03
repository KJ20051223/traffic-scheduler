"""Live payload progress; readers never acquire the download's quota lock."""
import copy
import math
import os
from pathlib import Path
import select
import sys
import time
from .core import Context, Error, configuration, read_json, write_json


def reporter(directory, data, requested=None, job_id=None, offset=0):
    base = data['download']['bytes']
    last = [0.0]
    def update(actual, elapsed, final=False):
        stamp = time.time()
        if not final and actual and stamp - last[0] < 1:
            return
        last[0] = stamp
        record = dict(pid=os.getpid(), id=job_id, requested_bytes=requested,
                      received_bytes=offset + actual, today_live_bytes=base + actual,
                      ledger_date=data['date'], sampled_at=stamp,
                      bytes_per_second=actual / elapsed if elapsed > 0 else 0,
                      status='between_requests' if final else 'receiving' if actual else 'connecting',
                      entry='新下载入口' if job_id else '兼容旧入口 · 当前分块')
        try:
            write_json(Path(directory) / 'progress.json', record)
        except OSError:
            pass  # Display failure must not interfere with quota settlement.
    return update


def finish(directory, status):
    path = Path(directory) / 'progress.json'
    try:
        record = read_json(path)
        if record and record.get('pid') == os.getpid():
            record.update(status=status, bytes_per_second=0, sampled_at=time.time())
            write_json(path, record)
    except (Error, OSError):
        pass


def alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False


def ratio_preview(ctx, item):
    from . import ratio
    state = read_json(ratio.state_path(ctx))
    if state is None:
        selected = ratio.auto_interfaces(ctx) if item['ratio_interface'] == 'auto' else ratio.names(item['ratio_interface'])
        current = ratio.snapshot(ctx, selected)
        result = ratio.view(item, ratio.initial_state(item, current, time.time()))
        result.update(baseline_persisted=False,
                      note='当前开机网卡累计值的只读预览；基线将在下载空闲时自动保存。重启前历史无法自动恢复，可通过累计流量校准补录。')
        return result
    ratio.check_state(state)
    if state['selector'] != item['ratio_interface']:
        raise Error('计量网卡已更改，请校准后继续')
    current = ratio.snapshot(ctx, list(state['raw']['interfaces']))
    projected = copy.deepcopy(state)
    for name, values in current['interfaces'].items():
        old = state['raw']['interfaces'][name]
        reset = current['boot'] != state['raw']['boot'] or values['index'] != old['index']
        for key in ('rx', 'tx'):
            projected[key] += values[key] if reset or values[key] < old[key] else values[key] - old[key]
    projected['sampled_at'] = time.time()
    result = ratio.view(item, projected)
    result['baseline_persisted'] = True
    return result


def snapshot(ctx, at=None):
    from . import traffic
    item = traffic.validate(configuration(ctx)['traffic'])
    current = at or traffic.moment(item['timezone'])
    directory = ctx.path(item['state_dir'])
    path = directory / ('quota-' + current.date().isoformat() + '.json')
    data = traffic.engine.read_day(path, current.date().isoformat())
    live = read_json(directory / 'progress.json', {})
    job = None
    active_record = read_json(traffic.task_directory(ctx) / 'active.json', {})
    identifier = active_record.get('id') or live.get('id')
    if isinstance(identifier, str) and len(identifier) == 32 and all(c in '0123456789abcdef' for c in identifier):
        job = read_json(traffic.task_directory(ctx) / 'jobs' / (identifier + '.json'))
    age = max(0, time.time() - live.get('sampled_at', 0))
    active = (live.get('status') in ('receiving', 'connecting', 'between_requests') and alive(live.get('pid'))
              and age < max(item['max_runtime'], 55) + 60)
    amount = live.get('received_bytes', 0) if active else (job or live).get('received_bytes', 0)
    total = (job or live).get('requested_bytes')
    speed = live.get('bytes_per_second', 0) if active else 0
    if type(speed) not in (int, float) or not math.isfinite(speed) or speed < 0:
        speed = 0
    today = data['download']['bytes']
    if active and live.get('ledger_date') == data['date']:
        today = max(today, live.get('today_live_bytes', today))
    budget = max(0, min(traffic.remaining(item, directory, data), item['daily_bytes'] - today))
    result = dict(progress_view=True, time=current.isoformat(), active=active,
                  status=live.get('status', 'idle') if active else (job or {}).get('status', 'idle'),
                  entry=live.get('entry', '暂无当前下载'), id=identifier, received_bytes=amount,
                  requested_bytes=total, percent=min(100, amount * 100 / total) if total else None,
                  task_remaining_bytes=max(0, total - amount) if total else None,
                  speed_mbps=speed * 8 / 1e6, configured_mbps=traffic.effective_rate(item),
                  eta_seconds=max(0, (total - amount) / speed) if total and speed else None,
                  today_bytes=today, today_settled_bytes=data['download']['bytes'],
                  daily_limit_bytes=item['daily_bytes'], remaining_bytes=budget,
                  paused=item['paused'], enabled=item['enabled'], strategy=item['strategy'],
                  update_age_seconds=round(age, 1) if live else None,
                  note='每秒刷新；实时已接收量尚待当前分块结算，最终配额以已结算字节为准。')
    if item['strategy'] == 'ratio':
        try:
            result['machine_traffic'] = ratio_preview(ctx, item)
            result['machine_traffic']['fillable_now_bytes'] = min(result['machine_traffic']['needed_bytes'], budget)
            result['machine_traffic']['limited_by_budget'] = result['machine_traffic']['needed_bytes'] > budget
        except (Error, OSError) as exc:
            result['machine_traffic'] = {'note': str(exc)}
    return result


def watch(ctx):
    from .menu import screen
    from .presentation import render_progress
    from .traffic import initialize_meter
    initialize_meter(ctx)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        render_progress(snapshot(ctx))
        return
    initialize_after = time.monotonic() + 5
    while True:
        if time.monotonic() >= initialize_after:
            initialize_meter(ctx)
            initialize_after = time.monotonic() + 5
        screen('实时下载进度')
        try:
            render_progress(snapshot(ctx))
        except (Error, OSError, ValueError) as exc:
            print('暂时无法读取进度：' + str(exc))
        print('\n输入 0 并回车返回；页面每秒刷新。')
        ready, _, _ = select.select([sys.stdin], [], [], 1)
        if ready:
            answer = sys.stdin.readline()
            if not answer or answer.strip() == '0':
                return False
