"""Persistent, host-interface traffic accounting. Call sample/seed under the quota lock."""
from fractions import Fraction
import math
import re
import time
from .core import Error, atomic, read_json, write_json


DEFAULTS = {'strategy': 'quota', 'ratio': '1:2', 'ratio_interface': 'auto',
            'ratio_tolerance_pct': 5.0, 'ratio_schedule': 'follow'}


def factor(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?', value):
        raise Error('比例格式为 上行:下行，例如 1:2')
    up, down = [Fraction(part.strip()) for part in value.split(':')]
    if up <= 0 or down <= 0 or not Fraction(1, 100) <= down / up <= 100:
        raise Error('比例必须为正数，下行/上行须在 0.01–100 之间')
    return down / up


def names(selector):
    values = selector.split(',') if isinstance(selector, str) else []
    if not values or len(values) != len(set(values)) or any(
            not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', name) or name in ('lo', '.', '..') for name in values):
        raise Error('网卡须为 auto 或真实出口网卡名称（多个用逗号分隔，不含 lo）')
    return values


def validate(item):
    for key, value in DEFAULTS.items():
        item.setdefault(key, value)
    if item['strategy'] not in ('quota', 'ratio') or item['ratio_schedule'] not in ('follow', 'random'):
        raise Error('strategy 须为 quota/ratio；ratio_schedule 须为 follow/random')
    factor(item['ratio'])
    names(item['ratio_interface'])
    tolerance = item['ratio_tolerance_pct']
    if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or not 0 <= tolerance <= 20:
        raise Error('比例容差须在 0–20% 之间')


def auto_interfaces(ctx):
    routes = []
    path = ctx.path('/proc/net/route')
    if path.exists():
        for line in path.read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 8 and fields[1] == '00000000' and fields[7] == '00000000':
                try:
                    if int(fields[3], 16) & 1 and fields[0] != 'lo':
                        routes.append((int(fields[6]), fields[0]))
                except ValueError:
                    continue
    if not routes:
        path = ctx.path('/proc/net/ipv6_route')
        if path.exists():
            for line in path.read_text().splitlines():
                fields = line.split()
                if len(fields) >= 10 and fields[0] == '0' * 32 and fields[1] == '00':
                    try:
                        if int(fields[8], 16) & 1 and fields[9] != 'lo':
                            routes.append((int(fields[5], 16), fields[9]))
                    except ValueError:
                        continue
    if not routes:
        raise Error('无法识别出口网卡；请配置 --ratio-interface eth0（按实际名称填写）')
    # One lowest-metric route, rather than summing bridges, tunnels and their parent.
    return names(sorted(routes)[0][1])


def snapshot(ctx, selected):
    try:
        boot = ctx.path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if not boot:
            raise ValueError('boot_id 为空')
        counters = {}
        for line in ctx.path('/proc/net/dev').read_text().splitlines():
            if ':' not in line:
                continue
            name, values = line.rsplit(':', 1)
            name, values = name.strip(), values.split()
            if name not in selected:
                continue
            if len(values) < 16:
                raise ValueError('网卡统计字段不完整')
            rx, tx = int(values[0]), int(values[8])
            if min(rx, tx) < 0:
                raise ValueError('网卡计数为负数')
            index = ctx.path('/sys/class/net/' + name + '/ifindex')
            counters[name] = {'rx': rx, 'tx': tx, 'index': int(index.read_text()) if index.exists() else 0}
        if set(counters) != set(selected):
            raise ValueError('所选网卡不存在；拒绝自动换卡或清零累计流量')
        return {'boot': boot, 'interfaces': counters}
    except (OSError, ValueError) as exc:
        raise Error('无法读取 Linux 网卡统计: ' + str(exc))


def state_path(ctx):
    return ctx.state / 'traffic' / 'machine-meter.json'


def check_state(state):
    try:
        if state['version'] != 1 or not isinstance(state['selector'], str):
            raise ValueError()
        for key in ('tx', 'rx'):
            if type(state[key]) is not int or state[key] < 0:
                raise ValueError()
        raw = state['raw']
        if not isinstance(raw['boot'], str) or not raw['interfaces']:
            raise ValueError()
        for name, values in raw['interfaces'].items():
            names(name)
            if any(type(values[k]) is not int or values[k] < 0 for k in ('tx', 'rx', 'index')):
                raise ValueError()
        if not isinstance(state['warnings'], list):
            raise ValueError()
        if type(state['calibrated']) is not bool or any(
                type(state[k]) not in (int, float) or not math.isfinite(state[k]) or state[k] < 0
                for k in ('started', 'sampled_at')):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise Error('机器流量计量状态损坏，拒绝清零；请检查状态备份后显式 ratio-seed 校准')


def view(item, state):
    multiplier = factor(item['ratio'])
    target = (state['tx'] * multiplier.numerator + multiplier.denominator - 1) // multiplier.denominator
    deficit = max(0, target - state['rx'])
    tolerance = Fraction(str(item['ratio_tolerance_pct'])) / 100
    within = state['rx'] * tolerance.denominator >= target * (tolerance.denominator - tolerance.numerator)
    need = 0 if within or deficit < 1_000_000 else deficit
    return dict(upload_bytes=state['tx'], download_bytes=state['rx'], target_download_bytes=target,
                needed_bytes=need, deficit_bytes=deficit, target_ratio=item['ratio'],
                actual_down_per_up=state['rx'] / state['tx'] if state['tx'] else None,
                interfaces=sorted(state['raw']['interfaces']), started=state['started'],
                sampled_at=state['sampled_at'], calibrated=state['calibrated'], warnings=state['warnings'],
                note='累计网卡 TX/RX（含业务、脚本和协议开销），不按日/月清零；不足 1 MB 不启动补流。')


def sample(ctx, item, timestamp=None):
    validate(item)
    timestamp = time.time() if timestamp is None else timestamp
    state = read_json(state_path(ctx))
    if state is not None:
        check_state(state)
        if state['selector'] != item['ratio_interface']:
            raise Error('计量网卡设置已变更，请用 traffic ratio-seed 校准累计流量后继续')
        selected = list(state['raw']['interfaces'])
    else:
        selected = auto_interfaces(ctx) if item['ratio_interface'] == 'auto' else names(item['ratio_interface'])
    current = snapshot(ctx, selected)
    if state is None:
        state = dict(version=1, selector=item['ratio_interface'], tx=sum(v['tx'] for v in current['interfaces'].values()),
                     rx=sum(v['rx'] for v in current['interfaces'].values()), raw=current, started=timestamp,
                     sampled_at=timestamp, calibrated=False,
                     warnings=['首次接入采用本次开机网卡累计值；此前重启丢失的历史未知，可用 ratio-seed 补录。'])
    else:
        previous = state['raw']
        reset = previous['boot'] != current['boot']
        for name, values in current['interfaces'].items():
            old = previous['interfaces'][name]
            replaced = reset or values['index'] != old['index']
            for key in ('tx', 'rx'):
                if replaced or values[key] < old[key]:
                    state[key] += values[key]
                    warning = '检测到重启、换卡或计数器重置；已保留累计值，最后采样后的缺失流量无法恢复。'
                    if warning not in state['warnings']:
                        state['warnings'].append(warning)
                else:
                    state[key] += values[key] - old[key]
        state['raw'] = current
        state['sampled_at'] = timestamp
    state['warnings'] = state['warnings'][-8:]
    write_json(state_path(ctx), state)
    return view(item, state)


def allowance(ctx, item):
    return sample(ctx, item)['needed_bytes'] if item['strategy'] == 'ratio' else 10**40


def seed(ctx, item, upload, download):
    """Explicit authoritative lifetime totals at the instant of this snapshot; no quota refund."""
    validate(item)
    if any(type(value) is not int or value < 0 for value in (upload, download)):
        raise Error('校准累计流量必须是非负字节数')
    selected = auto_interfaces(ctx) if item['ratio_interface'] == 'auto' else names(item['ratio_interface'])
    raw = snapshot(ctx, selected)
    stamp = time.time()
    state = dict(version=1, selector=item['ratio_interface'], tx=upload, rx=download, raw=raw,
                 started=stamp, sampled_at=stamp, calibrated=True, warnings=['累计流量已经手动校准；不改变脚本日/月预算计数。'])
    if state_path(ctx).exists():
        atomic(state_path(ctx).with_suffix('.previous.json'), state_path(ctx).read_bytes())
    write_json(state_path(ctx), state)
    return view(item, state)
