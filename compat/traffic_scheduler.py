#!/usr/bin/env python3
"""Traffic Scheduler: download-only daily quotas, rate limits and random time slices."""
import argparse
import contextlib
import datetime as dt
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import random
import re
import signal
import socket
import ssl
import sys
import tempfile
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

VERSION = '3.4.1'
PLAN_VERSION = 2
PLAN_WORK_SECONDS = 40
# Adjustable estimates, not measurements of real users or upload traffic.
DEFAULT_HOURLY_WEIGHTS = [0.4, 0.25, 0.15, 0.1, 0.1, 0.15, 0.35, 0.6,
                        0.9, 1.2, 1.4, 1.3, 1.0, 1.0, 1.4, 1.5,
                        1.4, 1.2, 1.0, 1.2, 1.5, 1.6, 1.2, 0.8]
CONFIG = Path('/etc/traffic-scheduler/config.json')
STATE = Path('/var/lib/traffic-scheduler')
BJ = dt.timezone(dt.timedelta(hours=8))
BLOCK = 32 * 1024
PIECE = 2 * 1024 * 1024
UA = 'traffic-scheduler/' + VERSION
DIRECTIONS = ('download',)
LABEL = {'download': '下行'}
ERRORS = (OSError, ValueError, http.client.HTTPException)

# Public download sources. Setup validates on the user's host.
# Independent mirror operators; each session shuffles a copy, preserving this list.
BUILTINS = {
    'download': {
        'cn': [
            {'name': '南京大学', 'kind': 'range', 'url': 'https://mirrors.nju.edu.cn/ubuntu-releases/24.04/ubuntu-24.04.4-live-server-amd64.iso'},
            {'name': '中国科学技术大学', 'kind': 'range', 'url': 'https://mirrors.ustc.edu.cn/ubuntu-releases/24.04/ubuntu-24.04.4-live-server-amd64.iso'},
            {'name': '清华大学', 'kind': 'range', 'url': 'https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/24.04/ubuntu-24.04.4-live-server-amd64.iso'},
            {'name': '北京外国语大学', 'kind': 'range', 'url': 'https://mirrors.bfsu.edu.cn/ubuntu-releases/24.04/ubuntu-24.04.4-live-server-amd64.iso'},
            {'name': '浙江大学', 'kind': 'range', 'url': 'https://mirrors.zju.edu.cn/ubuntu-releases/24.04/ubuntu-24.04.4-live-server-amd64.iso'},
            {'name': '中科院软件所', 'kind': 'range', 'url': 'https://mirror.iscas.ac.cn/ubuntu-releases/24.04/ubuntu-24.04.4-live-server-amd64.iso'},
        ],
        'overseas': [
            {'name': 'Cloudflare', 'kind': 'generated', 'url': 'https://speed.cloudflare.com/__down'},
        ],
    },
}


class Stop(Exception):
    pass


class DownloadHTTPError(OSError):
    def __init__(self, status, address, country, retry_after=None):
        super().__init__('下载 HTTP {}，目标 IP={} ({})'.format(status, address, country))
        self.status = status
        self.retry_after = retry_delay(retry_after)


def retry_delay(value):
    if not value:
        return 0
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            moment = parsedate_to_datetime(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=dt.timezone.utc)
            seconds = moment.timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, seconds) if math.isfinite(seconds) else 0


def transient(error):
    if getattr(error, 'original', None) is not None:
        return transient(error.original)
    if isinstance(error, DownloadHTTPError):
        return error.status in (408, 425, 429) or 500 <= error.status <= 599
    return not isinstance(error, (ValueError, ssl.SSLCertVerificationError))


def toolkit_home():
    path = Path('/opt/vps-toolkit/current')
    return path if (path / 'vps_toolkit').is_dir() and Path('/etc/vps-toolkit/config.json').is_file() else None


def now():
    toolkit = toolkit_home()
    if toolkit:
        sys.path.insert(0, str(toolkit))
        from vps_toolkit.core import Context, configuration
        from vps_toolkit.traffic import moment
        return moment(configuration(Context())['traffic']['timezone'])
    return dt.datetime.now(BJ)


def log(message):
    print('{} {}'.format(now().isoformat(timespec='seconds'), message), flush=True)


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    os.chmod(str(temporary), 0o600)
    if os.name == 'posix' and os.geteuid() == 0:
        owner = (path if path.exists() else path.parent).stat()
        os.chown(str(temporary), owner.st_uid, owner.st_gid)
    os.replace(str(temporary), str(path))
    if os.name == 'posix':
        descriptor = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def default_config():
    return {'version': 3, 'timezone': 'Asia/Shanghai', 'hourly_weights': list(DEFAULT_HOURLY_WEIGHTS), 'directions': {
        'download': {'enabled': True, 'daily_bytes': 20_000_000_000, 'mbps': 5, 'region': 'cn', 'targets': []},
    }}


def migrate_config(config):
    if config.get('version') != 2:
        return config
    # Preserve the ACTIVE old settings, not the unused download defaults (20 GB/10 Mbps).
    directions = config.get('directions', {})
    download = directions.get('download', {})
    if download.get('enabled'):
        item = dict(download)
    elif directions.get('upload', {}).get('enabled'):
        item = dict(directions['upload'])
        item['targets'] = []  # An upload receiver is not a downloadable file.
    else:
        raise ValueError('旧配置没有启用方向，请重新设置')
    item['enabled'] = True
    return {'version': 3, 'timezone': config.get('timezone'), 'directions': {'download': item}}


def parse_url(url):
    p = urlsplit(url)
    if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password or p.fragment:
        raise ValueError('目标必须是无用户名密码和片段的 HTTP(S) URL')
    if p.port is not None and not 1 <= p.port <= 65535:
        raise ValueError('端口无效')
    return p


def targets(config, direction):
    item = config['directions'][direction]
    return item.get('targets') or BUILTINS[direction][item['region']]


def shuffled_targets(config, direction, rng=None):
    result = list(targets(config, direction))
    (rng or random.SystemRandom()).shuffle(result)
    return result


def validate(config):
    if config.get('version') != 3 or config.get('timezone') != 'Asia/Shanghai':
        raise ValueError('配置版本或时区无效')
    if set(config.get('directions', {})) != {'download'}:
        raise ValueError('本版本仅支持下行，请重新设置或运行安装器迁移旧配置')
    weights = config.get('hourly_weights', DEFAULT_HOURLY_WEIGHTS)
    if (not isinstance(weights, list) or len(weights) != 24
            or any(type(w) not in (int, float) or not math.isfinite(w) or not 0 < w <= 1e6 for w in weights)):
        raise ValueError('小时权重须为 24 个大于 0、不超过 1000000 的有限数值，顺序对应北京时间 0–23 点')
    active_seconds = 0
    for direction in DIRECTIONS:
        item = config['directions'][direction]
        if item.get('enabled') is not True:
            raise ValueError('下行必须启用')
        if type(item.get('daily_bytes')) is not int or item['daily_bytes'] <= 0:
            raise ValueError('每日额度必须为正整数字节数')
        rate = item.get('mbps')
        if type(rate) not in (int, float) or not math.isfinite(rate) or not 0.01 <= rate <= 10000:
            raise ValueError('速度须在 0.01–10000 Mbps 之间')
        if item['region'] not in ('cn', 'overseas'):
            raise ValueError('区域无效')
        if not isinstance(item.get('targets', []), list):
            raise ValueError('targets 必须为列表')
        for target in targets(config, direction):
            parse_url(target['url'])
            if target.get('kind') not in ('range', 'generated', 'stream'):
                raise ValueError('目标 kind 无效')
        if item['enabled']:
            active_seconds += item['daily_bytes'] / (rate * 125000)
            if not targets(config, direction):
                raise ValueError('当前区域没有可用下载目标')
    if active_seconds > 86400 * 0.70:
        raise ValueError('按当前速度需运行 {:.1f} 小时，超过可安排的每日预算；请降低额度或提高速度'.format(active_seconds / 3600))
    return config


def load_config(path):
    return validate(migrate_config(json.loads(path.read_text(encoding='utf-8'))))


@contextlib.contextmanager
def locked(directory):
    import fcntl
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'run.lock').open('a') as handle:
        if os.name == 'posix' and os.geteuid() == 0:
            owner = directory.stat()
            os.fchown(handle.fileno(), owner.st_uid, owner.st_gid)
            os.fchmod(handle.fileno(), 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop('已有任务正在运行，请稍后再试')
        yield


def new_day(day):
    return {'date': day, 'download': {'bytes': 0, 'confirmed': 0, 'reserved': 0},
            'plan': None, 'last_tick': None}


def read_day(path, day):
    if not path.exists():
        return new_day(day)
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('date'), str):
        raise ValueError('计量记录损坏，拒绝自动清零')
    for direction in DIRECTIONS:
        if any(type(data.get(direction, {}).get(k)) is not int or data[direction][k] < 0
               for k in ('bytes', 'confirmed', 'reserved')):
            raise ValueError('计量记录损坏，拒绝自动清零')
        observed = data[direction].get('observed_bps')
        if observed is not None and (type(observed) not in (int, float)
                                     or not math.isfinite(observed) or observed <= 0):
            raise ValueError('计量记录中的速度损坏，拒绝自动清零')
    return data if data['date'] == day else new_day(day)


def recover(data):
    for direction in DIRECTIONS:
        entry = data[direction]
        if entry['reserved']:
            log('{}恢复未结算预留 {} 字节，保守计入额度'.format(LABEL[direction], entry['reserved']))
            entry['bytes'] += entry['reserved']
            entry['reserved'] = 0


def remaining_seconds(config, data):
    seconds = 0
    for direction in DIRECTIONS:
        item, entry = config['directions'][direction], data[direction]
        if item['enabled']:
            rate = item['mbps'] * 125000
            observed = entry.get('observed_bps')
            if type(observed) in (int, float) and math.isfinite(observed) and observed > 0:
                rate = min(rate, observed)
            seconds += max(0, item['daily_bytes'] - entry['bytes']) / rate
    return seconds


def weighted_minutes(config, available, count, rng):
    weights = config.get('hourly_weights', DEFAULT_HOURLY_WEIGHTS)
    # Exponential-race sampling: weighted choices without duplicate minutes.
    ranked = [(math.log(max(rng.random(), 1e-300)) / weights[n // 60], n) for n in available]
    return [n for _, n in sorted(ranked, reverse=True)[:count]]


def allocate_chunks(config, data, minutes, rng):
    remaining = max(0, config['directions']['download']['daily_bytes'] - data['download']['bytes'])
    if not minutes:
        return {}
    size, extra = divmod(remaining, len(minutes))
    larger = set(rng.sample(minutes, extra))
    return {str(minute): size + (minute in larger) for minute in minutes}


def make_plan(config, data, moment, rng=None):
    rng = rng or random.SystemRandom()
    minute = moment.hour * 60 + moment.minute
    first = minute + (data.get('last_tick') == minute)
    previous = data.get('plan') or {}
    not_before = previous.get('not_before') if previous.get('version') == PLAN_VERSION else None
    if type(not_before) is not int or not 10 <= not_before <= 60:
        not_before = rng.randint(10, 60)
    available = list(range(max(first, not_before), 1440))
    # Each chosen minute has its own payload quota, about 40 productive seconds.
    sessions = min(len(available), math.ceil(remaining_seconds(config, data) / PLAN_WORK_SECONDS))
    chosen = sorted(weighted_minutes(config, available, sessions, rng))
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    return {'version': PLAN_VERSION, 'minutes': chosen, 'not_before': not_before,
            'chunks': allocate_chunks(config, data, chosen, rng), 'config_hash': fingerprint}


def due(config, data, moment):
    if remaining_seconds(config, data) <= 0:
        return False
    minute = moment.hour * 60 + moment.minute
    plan = data['plan']
    return (minute >= plan.get('not_before', 0) and minute in plan['minutes']
            and plan.get('chunks', {}).get(str(minute), 0) > 0)


def extend_plan(config, data, moment, rng=None):
    """Randomly add slices and redistribute missed payload over future chunks."""
    rng = rng or random.SystemRandom()
    minute = moment.hour * 60 + moment.minute
    planned = set(data['plan']['minutes'])
    first = minute + (data.get('last_tick') == minute)
    future = set(range(max(first, data['plan'].get('not_before', 0)), 1440))
    needed = min(len(future), math.ceil(remaining_seconds(config, data) / PLAN_WORK_SECONDS))
    extra = needed - len(planned & future)
    changed = False
    if extra > 0:
        planned.update(weighted_minutes(config, sorted(future - planned), extra, rng))
        data['plan']['minutes'] = sorted(planned)
        changed = True
    active = sorted(planned & future)
    remaining = max(0, config['directions']['download']['daily_bytes'] - data['download']['bytes'])
    chunks = data['plan'].get('chunks', {})
    if changed or sum(chunks.get(str(n), 0) for n in active) != remaining:
        data['plan']['chunks'] = allocate_chunks(config, data, active, rng)
        changed = True
    return changed


def json_get(url, limit=256000):
    p = parse_url(url)
    if p.scheme != 'https':
        raise ValueError('元数据查询必须使用 HTTPS')
    conn = http.client.HTTPSConnection(p.hostname, p.port, timeout=10)
    try:
        conn.request('GET', p.path + ('?' + p.query if p.query else ''), headers={'User-Agent': UA})
        response = conn.getresponse()
        if response.status != 200:
            raise OSError('元数据服务 HTTP {}'.format(response.status))
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('元数据响应过大')
        return json.loads(raw)
    finally:
        conn.close()


class Network:
    def __init__(self, cache=None):
        self.cache = cache if cache is not None else {}
        self.context = ssl.create_default_context()
        self.preferred = {}

    def resolve(self, host, port):
        try:
            addresses = list(dict.fromkeys(r[4][0] for r in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)))
            addresses = [ip for ip in addresses if ipaddress.ip_address(ip).is_global]
        except socket.gaierror:
            addresses = []
        if not addresses:
            # Handles local proxy fake-IP DNS without altering system resolver settings.
            for base in ('https://dns.alidns.com/resolve?', 'https://dns.google/resolve?'):
                try:
                    data = json_get(base + urlencode({'name': host, 'type': 'A'}))
                    addresses = [x['data'] for x in data.get('Answer', [])
                                 if x.get('type') == 1 and ipaddress.ip_address(x['data']).is_global]
                    if addresses:
                        break
                except ERRORS:
                    continue
        if not addresses:
            raise OSError('无法解析公网 IPv4: ' + host)
        return addresses

    def country(self, address):
        cached = self.cache.get(address)
        if cached and time.time() - cached['at'] < 86400:
            return cached['country']
        data = json_get('https://ipwho.is/' + address)
        if data.get('success') is not True or data.get('ip') != address:
            raise OSError('IP 归属查询失败: ' + address)
        country = data.get('country_code')
        if not isinstance(country, str) or len(country) != 2:
            raise ValueError('IP 归属响应无效')
        self.cache[address] = {'country': country, 'at': time.time()}
        return country

    def connect(self, url, region):
        p = parse_url(url)
        port = p.port or (443 if p.scheme == 'https' else 80)
        last = OSError('没有符合所选地区的公网 IPv4: ' + p.hostname)
        addresses = self.resolve(p.hostname, port)
        preferred = self.preferred.get((p.hostname, port))
        if preferred in addresses:
            addresses.remove(preferred)
            addresses.insert(0, preferred)
        for address in addresses[:5]:
            raw = conn = None
            try:
                country = self.country(address)
                if (country == 'CN') != (region == 'cn'):
                    last = OSError('{} 归属 {}，与选择区域不符'.format(address, country))
                    continue
                raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                raw.settimeout(10)
                raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, BLOCK)
                raw.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, BLOCK)
                raw.connect((address, port))
                if p.scheme == 'https':
                    raw = self.context.wrap_socket(raw, server_hostname=p.hostname)
                conn = http.client.HTTPConnection(p.hostname, port, timeout=10)
                conn.sock = raw
                self.preferred[(p.hostname, port)] = address
                return conn, p, address, country
            except ERRORS as error:
                if raw is not None:
                    raw.close()
                if conn is not None:
                    conn.close()
                last = error
        raise OSError(str(last))


class Pacer:
    def __init__(self, mbps):
        self.rate = mbps * 125000
        self.due = time.monotonic()

    def wait(self, size):
        self.due = max(self.due, time.monotonic()) + size / self.rate
        time.sleep(max(0, self.due - time.monotonic()))


def request_path(p):
    return (p.path or '/') + ('?' + p.query if p.query else '')


def download_url(target, amount):
    p = parse_url(target['url'])
    if target['kind'] == 'generated':
        query = dict(parse_qsl(p.query))
        query['bytes'] = str(amount)
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ''))
    return target['url']


def transfer(network, direction, item, target, amount, directory, data, path, pacer, offset=0, hasher=None, progress=None):
    if direction != 'download':
        raise ValueError('本版本仅支持下行下载')
    if hasattr(pacer, 'transfer'):
        return pacer.transfer(network, direction, item, target, amount, directory, data, path,
                              offset=offset, hasher=hasher, progress=progress)
    entry = data[direction]
    entry['reserved'] = amount
    atomic_json(path, data)
    actual = 0
    confirmed = False
    started = time.monotonic()
    if progress is None and toolkit_home():
        from vps_toolkit.progress import reporter
        progress = reporter(directory, data, requested=amount)
    if progress:
        progress(0, 0)
    conn = response = None
    url = download_url(target, amount)
    try:
        for _ in range(5):
            conn, p, address, country = network.connect(url, item['region'])
            headers = {'User-Agent': UA, 'Accept-Encoding': 'identity', 'Connection': 'close'}
            headers['Range'] = 'bytes={}-{}'.format(offset, offset + amount - 1)
            conn.request('GET', request_path(p), headers=headers)
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location:
                    raise OSError('重定向缺少目标')
                url = urljoin(url, location)
                parse_url(url)
                response.close()
                conn.close()
                continue
            if response.status not in (200, 206):
                raise DownloadHTTPError(response.status, address, country, response.getheader('Retry-After'))
            if 'html' in response.getheader('Content-Type', '').lower():
                raise OSError('下载端返回网页，可能被限制访问')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise OSError('不接受压缩传输')
            expected = amount
            if response.status == 206:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.getheader('Content-Range', ''))
                if not match:
                    raise OSError('Range 响应无效')
                first, last, total = map(int, match.groups())
                if first != offset or not 0 <= last < total or last + 1 > offset + amount:
                    raise OSError('Range 响应越界')
                expected = last - first + 1
            elif target['kind'] == 'range' or offset and target['kind'] != 'generated':
                raise OSError('镜像源未遵守 Range，已中止并切换')
            elif response.getheader('Content-Length'):
                expected = min(amount, int(response.getheader('Content-Length')))
            if expected <= 0:
                raise OSError('下载内容为空')
            with tempfile.TemporaryFile(prefix='payload-', dir=str(directory)) as output:
                while actual < expected:
                    chunk = response.read1(min(BLOCK, expected - actual))
                    if not chunk:
                        raise OSError('下载提前中断')
                    actual += len(chunk)
                    if hasher is not None:
                        hasher.update(chunk)
                    output.write(chunk)
                    pacer.wait(len(chunk))
                    if progress:
                        progress(actual, time.monotonic() - started)
            confirmed = True
            break
        else:
            raise OSError('下载重定向过多')
        log('{} {} 字节，目标={}，IP={} ({})，临时内容已清理'.format(
            LABEL[direction], actual, target['name'], address, country))
        return actual
    finally:
        if response is not None:
            response.close()
        if conn is not None:
            conn.close()
        entry['bytes'] += actual
        entry['confirmed'] += actual if confirmed else 0
        elapsed = time.monotonic() - started
        if actual > 0 and elapsed > 0:
            measured = actual / elapsed
            previous = entry.get('observed_bps', measured)
            entry['observed_bps'] = previous * 0.5 + measured * 0.5
        entry['reserved'] = 0
        atomic_json(path, data)
        if progress:
            progress(actual, elapsed, final=True)


def stop_signal(signum, frame):
    raise Stop('时段结束或收到停止信号')


@contextlib.contextmanager
def deadline(seconds):
    if not hasattr(signal, 'setitimer'):
        yield
        return
    old = {}
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM):
        old[signum] = signal.signal(signum, stop_signal)
    signal.setitimer(signal.ITIMER_REAL, max(0.01, seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for signum, handler in old.items():
            signal.signal(signum, handler)


def pick_direction(config, data, metric='bytes'):
    choices = [d for d in DIRECTIONS if config['directions'][d]['enabled']
               and data[d][metric] < config['directions'][d]['daily_bytes']]
    return min(choices, key=lambda d: data[d][metric] / config['directions'][d]['daily_bytes']) if choices else None


def run_session(config, data, path, directory, network, metric='bytes', health=None, rng=None, max_bytes=None):
    health = health if health is not None else {}
    rng = rng or random.SystemRandom()
    pools = {d: shuffled_targets(config, d, rng) for d in DIRECTIONS}
    selected = {}
    failed = {d: set() for d in DIRECTIONS}
    attempts = {}
    pacers = {d: Pacer(config['directions'][d]['mbps']) for d in DIRECTIONS}
    if toolkit_home():
        from vps_toolkit.downloader import legacy_session
        pacers['download'] = legacy_session()
    stop_at = None if max_bytes is None else data['download'][metric] + max_bytes
    while True:
        if stop_at is not None and data['download'][metric] >= stop_at:
            return True
        direction = pick_direction(config, data, metric)
        if direction is None:
            return True
        if hasattr(pacers[direction], 'wait_ready'):
            from vps_toolkit.progress import reporter
            pacers[direction].wait_ready(directory, reporter(directory, data))
        item = config['directions'][direction]
        candidates = [t for t in pools[direction] if t['url'] not in failed[direction]]
        available = [t for t in candidates if health.get(t['url'], {}).get('until', 0) <= time.time()]
        if not available:
            if candidates:
                wait = max(0, min(health.get(t['url'], {}).get('until', 0) for t in candidates) - time.time())
                if wait <= 15:
                    log('暂不可用，等待 {:.1f} 秒后按随机候选顺序重试'.format(wait))
                    time.sleep(max(0.01, wait))
                    continue
                raise OSError('所有下载源均在冷却，最早约 {:.0f} 秒后可重试；请稍后 test 或更换目标'.format(wait))
            raise OSError('{}所有目标均失败（临时错误每源最多尝试 3 次）；请稍后 test 或更换目标'.format(LABEL[direction]))
        target = selected.get(direction, available[0])
        if target not in available:
            target = available[0]
        if selected.get(direction) != target:
            log('本次随机候选选择：{}（共 {} 个目标，失败自动换源）'.format(target['name'], len(pools[direction])))
        # At a low configured speed, finish a request inside a short time slice.
        capacity = pacers[direction].batch_bytes() if hasattr(pacers[direction], 'batch_bytes') else min(PIECE, max(1, int(item['mbps'] * 125000 * 8)))
        amount = min(capacity,
                     item['daily_bytes'] - data[direction][metric])
        if stop_at is not None:
            amount = min(amount, stop_at - data[direction][metric])
        if toolkit_home():
            # Both schedulers hold this same quota lock; never nest the lock here.
            from vps_toolkit.traffic import legacy_allowance
            amount = min(amount, legacy_allowance(config, data, directory))
            if amount <= 0:
                return True
        before = data[direction]['confirmed']
        try:
            transfer(network, direction, item, target, amount, directory, data, path, pacers[direction])
            selected[direction] = target
            if data[direction]['confirmed'] <= before:
                raise OSError('没有实际确认的数据传输')
            health.pop(target['url'], None)
        except ERRORS as error:
            log('{}目标失败 {}: {}'.format(LABEL[direction], target['name'], error))
            url = target['url']
            attempts[url] = attempts.get(url, 0) + 1
            retryable = transient(error)
            if retryable and attempts[url] < 3:
                delay = (2 if attempts[url] == 1 else 5) + rng.uniform(0, 1)
                delay = max(delay, getattr(error, 'retry_after', 0))
                log('将换源或在 {:.1f} 秒后重试 {}（本时段第 {}/3 次尝试已失败）'.format(
                    delay, target['name'], attempts[url]))
            else:
                delay = 300 if retryable else 1800
                delay = max(delay, getattr(error, 'retry_after', 0))
                failed[direction].add(url)
                log('{}本时段跳过，冷却 {} 秒'.format(target['name'], delay))
            health[url] = {'until': time.time() + delay, 'error': str(error),
                           'status': getattr(error, 'status', None)}
            selected.pop(direction, None)


def load_geo(directory):
    path = directory / 'geo.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_health(directory):
    try:
        data = json.loads((directory / 'source-health.json').read_text(encoding='utf-8'))
        return {url: entry for url, entry in data.items()
                if isinstance(entry, dict) and type(entry.get('until')) in (int, float)
                and math.isfinite(entry['until']) and entry['until'] > time.time()}
    except (OSError, ValueError, AttributeError):
        return {}


def tick(config, directory, rng=None):
    rng = rng or random.SystemRandom()
    with locked(directory):
        verified = json.loads((directory / 'last-test.json').read_text(encoding='utf-8'))
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        if verified.get('config_hash') != fingerprint:
            raise ValueError('当前配置尚未实测，请先运行 test')
        moment = now()
        path = directory / ('quota-' + moment.date().isoformat() + '.json')
        data = read_day(path, moment.date().isoformat())
        recover(data)
        if (not data.get('plan') or data['plan'].get('config_hash') != fingerprint
                or data['plan'].get('version') != PLAN_VERSION):
            data['plan'] = make_plan(config, data, moment, rng)
            atomic_json(path, data)
            gate = data['plan']['not_before']
            log('今日随机分块计划生成：{} 个下载小块，最早允许 {:02d}:{:02d} 开始'.format(
                len(data['plan']['minutes']), gate // 60, gate % 60))
            if remaining_seconds(config, data) > (1440 - max(moment.hour * 60 + moment.minute, gate)) * PLAN_WORK_SECONDS:
                log('今日剩余时间可能不足以刷足；将尽力传输，午夜按新日期计量，不虚报完成')
        elif extend_plan(config, data, moment, rng):
            atomic_json(path, data)
        allowance = None
        follow_ratio = False
        toolkit = toolkit_home()
        if toolkit:
            sys.path.insert(0, str(toolkit))
            try:
                from vps_toolkit.traffic import legacy_allowance, legacy_realtime
                allowance = legacy_allowance(config, data, directory)
                follow_ratio = legacy_realtime()
            except Exception as error:
                raise ValueError('统一预算检查失败，停止下载: ' + str(error))
            if allowance <= 0:
                return 0
        key = moment.hour * 60 + moment.minute
        if data.get('last_tick') == key or (not follow_ratio and not due(config, data, moment)):
            return 0
        data['last_tick'] = key
        atomic_json(path, data)
        until_midnight = (moment.replace(hour=0, minute=0, second=0, microsecond=0)
                          + dt.timedelta(days=1) - now()).total_seconds()
        seconds = min(52 - now().second - now().microsecond / 1e6, until_midnight)
        if seconds < 3:
            return 0
        network = Network(load_geo(directory))
        health = load_health(directory)
        item = config['directions']['download']
        chunk_bytes = (max(1, int(item['mbps'] * 125000 * PLAN_WORK_SECONDS)) if follow_ratio
                       else data['plan']['chunks'][str(key)])
        if allowance is not None:
            chunk_bytes = min(chunk_bytes, allowance)
        log('执行下行：每日 {:g} GB，限速 {:g} Mbps，区域 {}；今日已计量 {:.6f} GB'.format(
            item['daily_bytes'] / 1e9, item['mbps'], item['region'], data['download']['bytes'] / 1e9))
        log('本次小块额度 {:.3f} MB，完成后等待下个随机时段'.format(chunk_bytes / 1e6))
        try:
            with deadline(seconds):
                jitter = min(rng.uniform(0, 5), max(0, seconds - 3))
                time.sleep(jitter)
                run_session(config, data, path, directory, network, health=health, max_bytes=chunk_bytes)
                if data['download']['bytes'] >= item['daily_bytes']:
                    log('今日配置的计量额度已用完，已确认接收量可通过 status 查看')
                else:
                    log('本次小块下载完成，等待下个随机时段')
        except Stop:
            log('本次随机时段结束，下行={:.6f} GB'.format(data['download']['bytes'] / 1e9))
        finally:
            atomic_json(directory / 'geo.json', network.cache)
            atomic_json(directory / 'source-health.json', health)
        return 0


def real_test(config, directory, megabytes=10, rng=None):
    # Independent test ledger: never resets or spends the scheduled quota silently.
    directory.mkdir(parents=True, exist_ok=True)
    size = int(megabytes * 1_000_000)
    test_config = json.loads(json.dumps(config))
    for d in DIRECTIONS:
        test_config['directions'][d]['daily_bytes'] = size
    network = Network(load_geo(directory))
    health = load_health(directory)
    item = config['directions']['download']
    log('仅下行实测 {:g} MB；配置每日 {:g} GB，限速 {:g} Mbps，区域 {}'.format(
        megabytes, item['daily_bytes'] / 1e9, item['mbps'], item['region']))
    with tempfile.TemporaryDirectory(prefix='test-', dir=str(directory)) as temporary:
        test_dir = Path(temporary)
        data = new_day(now().date().isoformat())
        start = time.monotonic()
        try:
            with deadline(max(180, remaining_seconds(test_config, data) * 4 + 60)):
                run_session(test_config, data, test_dir / 'test.json', test_dir, network,
                            metric='confirmed', health=health, rng=rng)
        finally:
            atomic_json(directory / 'geo.json', network.cache)
            atomic_json(directory / 'source-health.json', health)
        for d in DIRECTIONS:
            if test_config['directions'][d]['enabled'] and data[d]['confirmed'] < size:
                raise OSError('{}实测未完成：确认 {} / {} 字节'.format(LABEL[d], data[d]['confirmed'], size))
        results = '，'.join('{}={} MB'.format(LABEL[d], data[d]['confirmed'] / 1e6)
                          for d in DIRECTIONS if test_config['directions'][d]['enabled'])
        log('实测通过：{}，耗时 {:.1f} 秒；测试流量单独计量'.format(results, time.monotonic() - start))
        atomic_json(directory / 'last-test.json', {'at': now().isoformat(), 'version': VERSION,
                    'download': data['download'],
                    'config_hash': hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()})


def prompt(text, default):
    answer = input('{} [{}]: '.format(text, default)).strip()
    return answer or str(default)


def configure(path, args):
    config = load_config(path) if path.exists() else default_config()
    config.setdefault('hourly_weights', list(DEFAULT_HOURLY_WEIGHTS))
    item = config['directions']['download']
    explicit = args.keep or args.builtin or any(value is not None for value in
                                               (args.gb, args.mbps, args.region, args.url, args.hourly_weights))
    if not explicit:
        print('\n仅下行设置（北京时间，全天随机分散；GB=10^9 字节，Mbps=兆比特/秒）')
        args.gb = float(prompt('每日下载流量 GB', item['daily_bytes'] / 1e9))
        args.mbps = float(prompt('下载速度上限 Mbps', item['mbps']))
        region = prompt('目标：1 中国大陆 / 2 海外（含港澳台）', '2' if item['region'] == 'overseas' else '1')
        if region not in ('1', '2'):
            raise ValueError('目标只能选 1 或 2')
        args.region = 'cn' if region == '1' else 'overseas'
        current = item.get('targets', [])
        custom = prompt('自定义下载 HTTP(S) 地址（输入 builtin 使用内置）', current[0]['url'] if current else '').strip()
        args.url = custom if custom and custom != 'builtin' else None
        args.builtin = not args.url
        weights = prompt('小时权重：estimated 估算 / uniform 均匀 / 24个逗号分隔数值；keep 保留', 'keep').strip()
        if weights != 'keep':
            args.hourly_weights = weights
    if args.hourly_weights is not None:
        if args.hourly_weights == 'estimated':
            config['hourly_weights'] = list(DEFAULT_HOURLY_WEIGHTS)
        elif args.hourly_weights == 'uniform':
            config['hourly_weights'] = [1] * 24
        else:
            config['hourly_weights'] = [float(w.strip()) for w in args.hourly_weights.split(',')]
    if args.gb is not None:
        quota = args.gb
        if not math.isfinite(quota) or quota <= 0 or not math.isfinite(quota * 1e9):
            raise ValueError('每日流量必须是有限正数')
        item['daily_bytes'] = int(quota * 1e9)
    if args.mbps is not None:
        item['mbps'] = args.mbps
    if args.region is not None:
        if args.region != item['region']:
            item['targets'] = []
        item['region'] = args.region
    if args.builtin:
        item['targets'] = []
    if args.url is not None:
        parse_url(args.url)
        item['targets'] = [{'name': '自定义下载目标', 'url': args.url, 'kind': 'stream'}]
    validate(config)
    atomic_json(path, config)
    if os.name == 'posix' and os.geteuid() == 0:
        import grp
        try:
            os.chown(str(path), 0, grp.getgrnam('traffic-scheduler').gr_gid)
            os.chmod(str(path), 0o640)
        except KeyError:
            pass
    saved = load_config(path)
    if saved != config:
        raise OSError('配置回读不一致，拒绝继续')
    item = saved['directions']['download']
    print('\n配置已保存并回读确认：' + str(path.resolve()))
    print('仅下行 | 每日 {:g} GB | 限速 {:g} Mbps | 区域 {} | {}'.format(
        item['daily_bytes'] / 1e9, item['mbps'], item['region'], '自定义目标' if item['targets'] else '内置目标'))
    print('需要真实下载测试通过后再启用定时任务；修改设置不会清零当天进度。')
    print('下载源：{} 个，本次测试和每个调度时段均随机排列，失败自动换源。'.format(len(targets(saved, 'download'))))
    print('按小时权重随机分块：{}；权重仅控制时间分配，不改变限速和每日总额度。'.format(weight_profile(saved)))


def weight_profile(config):
    weights = config.get('hourly_weights', DEFAULT_HOURLY_WEIGHTS)
    if weights == DEFAULT_HOURLY_WEIGHTS:
        return '默认估算作息（非真实用户统计）'
    return '全天均匀' if weights == [1] * 24 else '自定义'


def status(config, directory):
    data = read_day(directory / ('quota-' + now().date().isoformat() + '.json'), now().date().isoformat())
    print('Traffic Scheduler {} | 北京时间 {}'.format(VERSION, now().isoformat(timespec='seconds')))
    print('时间权重: ' + weight_profile(config))
    print('北京时间 0–23 点权重: ' + ','.join('{:g}'.format(w) for w in config.get('hourly_weights', DEFAULT_HOURLY_WEIGHTS)))
    for d in DIRECTIONS:
        item = config['directions'][d]
        print('{}: {} | {:.3f}/{:.3f} GB | 已确认 {:.3f} GB | 预留 {:.3f} MB | {} Mbps | {}'.format(
            LABEL[d], '启用' if item['enabled'] else '关闭', data[d]['bytes'] / 1e9,
            item['daily_bytes'] / 1e9, data[d]['confirmed'] / 1e9,
            data[d]['reserved'] / 1e6, item['mbps'], item['region']))
    plan = data.get('plan')
    if plan:
        minute = now().hour * 60 + now().minute
        future = [n for n in plan['minutes'] if n > minute]
        if plan.get('version') == PLAN_VERSION:
            gate = plan['not_before']
            print('今日随机分块：{} 个时段；最早允许 {:02d}:{:02d} 开始'.format(
                len(plan['minutes']), gate // 60, gate % 60))
        print('下次计划时段: ' + ('{:02d}:{:02d}'.format(future[0] // 60, future[0] % 60) if future else '今日无剩余随机时段，必要时自动补足'))
        if future and plan.get('chunks', {}).get(str(future[0])):
            print('下个小块额度: {:.3f} MB'.format(plan['chunks'][str(future[0])] / 1e6))
    print('额度按有效负载计量；协议开销、重传、首次/手动实测另计。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=VERSION)
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('--state-dir', type=Path, default=STATE)
    commands = parser.add_subparsers(dest='command', required=True)
    settings = commands.add_parser('configure', help='仅下行设置；无参数交互，修改后需要 test')
    settings.add_argument('--gb', type=float, help='每日下载 GB 数')
    settings.add_argument('--mbps', type=float, help='下载速度上限 Mbps')
    settings.add_argument('--region', choices=('cn', 'overseas'))
    settings.add_argument('--hourly-weights', help='estimated 估算、uniform 均匀，或 24 个逗号分隔的正数（北京时间 0–23 点）')
    target = settings.add_mutually_exclusive_group()
    target.add_argument('--url', help='自定义下载文件 HTTP(S) URL')
    target.add_argument('--builtin', action='store_true', help='使用所选区域内置目标')
    settings.add_argument('--keep', action='store_true', help='保留已有数值并迁移为仅下行；首次使用默认值')
    commands.add_parser('tick', help='执行当前随机时间片')
    commands.add_parser('status', help='显示今日下行进度')
    commands.add_parser('sources', help='显示当前下载源和冷却状态')
    test = commands.add_parser('test', help='任何时间真实下载，默认 10MB')
    test.add_argument('--mb', type=float, default=10)
    args = parser.parse_args()
    if args.command == 'configure':
        configure(args.config, args)
        return 0
    config = load_config(args.config)
    print('读取配置：' + str(args.config.resolve()), flush=True)
    if args.command == 'status':
        status(config, args.state_dir)
        return 0
    if args.command == 'sources':
        health = load_health(args.state_dir)
        for item in targets(config, 'download'):
            entry = health.get(item['url'], {})
            wait = max(0, entry.get('until', 0) - time.time())
            print('{} | {} | {}'.format(item['name'], '冷却 {:.0f} 秒'.format(wait) if wait else '可尝试', item['url']))
            if wait:
                print('  上次错误：' + str(entry.get('error', '未知')))
        return 0
    if args.command == 'test':
        if not math.isfinite(args.mb) or not 0.001 <= args.mb <= 1000:
            raise ValueError('测试量必须在 0.001–1000 MB 之间')
        toolkit = toolkit_home()
        if toolkit:
            sys.path.insert(0, str(toolkit))
            from vps_toolkit.traffic import legacy_test
            legacy_test(config, args.state_dir, args.mb)
            return 0
        with locked(args.state_dir):
            real_test(config, args.state_dir, args.mb)
        return 0
    return tick(config, args.state_dir)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Stop as error:
        log(str(error))
        sys.exit(1)
    except (KeyboardInterrupt,) + ERRORS as error:
        log('失败: ' + str(error))
        sys.exit(1)
