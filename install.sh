#!/usr/bin/env bash
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive
defaults=0
uninstall=0
purge=0
for option in "$@"; do
    case "$option" in
        --defaults) defaults=1 ;;
        --uninstall) uninstall=1 ;;
        --purge) purge=1 ;;
        *) echo '用法: sudo bash install.sh [--defaults | --uninstall [--purge]]' >&2; exit 2 ;;
    esac
done
[[ $EUID -eq 0 ]] || { echo '请使用 sudo bash install.sh' >&2; exit 1; }
[[ -d /run/systemd/system ]] || { echo '需要启用了 systemd 的 Ubuntu 20.04+' >&2; exit 1; }
if [[ $uninstall -eq 1 ]]; then
    systemctl disable --now traffic-scheduler.timer 2>/dev/null || true
    systemctl stop traffic-scheduler.service 2>/dev/null || true
    rm -f -- /etc/systemd/system/traffic-scheduler.timer /etc/systemd/system/traffic-scheduler.service
    rm -f -- /usr/local/bin/traffic-scheduler
    systemctl daemon-reload
    if [[ $purge -eq 1 ]]; then
        rm -rf -- /etc/traffic-scheduler /var/lib/traffic-scheduler
    fi
    echo '已停止并卸载。未指定 --purge 时保留配置和计量记录。'
    exit 0
fi
[[ $purge -eq 0 ]] || { echo '--purge 仅与 --uninstall 一起使用' >&2; exit 2; }
. /etc/os-release
[[ ${ID:-} == ubuntu ]] && dpkg --compare-versions "${VERSION_ID:-0}" ge 20.04 || {
    echo '支持 Ubuntu 20.04 或更新版本' >&2; exit 1;
}
if [[ $defaults -eq 0 && ! -t 0 ]]; then
    echo '交互安装需要终端。请先下载 install.sh 再 sudo bash install.sh，或使用 --defaults。' >&2
    exit 1
fi
apt-get update
apt-get install -y --no-install-recommends python3 ca-certificates tzdata
if ! id -u traffic-scheduler >/dev/null 2>&1; then
    useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin traffic-scheduler
fi
# Stop both versions before configuration so a failed first test cannot leave a timer running.
systemctl disable --now traffic-scheduler.timer 2>/dev/null || true
systemctl stop traffic-scheduler.service 2>/dev/null || true
systemctl disable --now cn-traffic.timer 2>/dev/null || true
systemctl stop cn-traffic.service 2>/dev/null || true
rm -f -- /etc/systemd/system/cn-traffic.timer /etc/systemd/system/cn-traffic.service /usr/local/bin/cn-traffic
install -d -m 0755 /usr/local/bin
install -d -m 0750 -o root -g traffic-scheduler /etc/traffic-scheduler
install -d -m 0700 -o traffic-scheduler -g traffic-scheduler /var/lib/traffic-scheduler
cat > /usr/local/bin/traffic-scheduler <<'TRAFFIC_SCHEDULER_PY'
#!/usr/bin/env python3
"""Traffic Scheduler: bounded, geofenced, randomly distributed daily transfers."""
import argparse
import contextlib
import datetime as dt
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

VERSION = '2.0.0'
CONFIG = Path('/etc/traffic-scheduler/config.json')
STATE = Path('/var/lib/traffic-scheduler')
BJ = dt.timezone(dt.timedelta(hours=8))
BLOCK = 32 * 1024
PIECE = 2 * 1024 * 1024
UA = 'traffic-scheduler/' + VERSION
DIRECTIONS = ('upload', 'download')
LABEL = {'upload': '上行', 'download': '下行'}
ERRORS = (OSError, ValueError, http.client.HTTPException)

# Public download sources and speed-test endpoints. Setup validates on the user's host.
# Failed/restricted HUST/USTC/SJTU endpoints deliberately are not in these defaults.
BUILTINS = {
    'download': {
        'cn': [
            {'name': '华为云镜像', 'kind': 'range', 'url': 'https://mirrors.huaweicloud.com/ubuntu-releases/24.04/ubuntu-24.04.4-live-server-amd64.iso'},
            {'name': '华为云仓库', 'kind': 'range', 'url': 'https://repo.huaweicloud.com/ubuntu-releases/24.04/ubuntu-24.04.4-live-server-amd64.iso'},
        ],
        'overseas': [
            {'name': 'Cloudflare', 'kind': 'generated', 'url': 'https://speed.cloudflare.com/__down'},
        ],
    },
    'upload': {
        'cn': [
            {'name': '昆山杜克测速节点', 'kind': 'post', 'url': 'https://speedtest.dukekunshan.edu.cn:8080/upload'},
        ],
        'overseas': [
            {'name': 'Cloudflare', 'kind': 'post', 'url': 'https://speed.cloudflare.com/__up'},
        ],
    },
}


class Stop(Exception):
    pass


def now():
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
    os.replace(str(temporary), str(path))
    if os.name == 'posix':
        descriptor = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def default_config():
    return {'version': 2, 'timezone': 'Asia/Shanghai', 'directions': {
        'upload': {'enabled': True, 'daily_bytes': 20_000_000_000, 'mbps': 10, 'region': 'cn', 'targets': []},
        'download': {'enabled': False, 'daily_bytes': 20_000_000_000, 'mbps': 10, 'region': 'cn', 'targets': []},
    }}


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


def validate(config):
    if config.get('version') != 2 or config.get('timezone') != 'Asia/Shanghai':
        raise ValueError('配置版本或时区无效')
    active_seconds = 0
    for direction in DIRECTIONS:
        item = config['directions'][direction]
        if type(item.get('enabled')) is not bool:
            raise ValueError('enabled 必须为布尔值')
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
            if target.get('kind') not in ('range', 'generated', 'post', 'stream', 'receiver'):
                raise ValueError('目标 kind 无效')
        if item['enabled']:
            active_seconds += item['daily_bytes'] / (rate * 125000)
            if not targets(config, direction):
                raise ValueError('{}尚无可用内置目标，请输入已授权的自定义上传接收 URL'.format(LABEL[direction]))
    if not any(config['directions'][d]['enabled'] for d in DIRECTIONS):
        raise ValueError('至少启用一个方向')
    # A single worker alternates directions, preserving each configured speed ceiling.
    if active_seconds > 86400 * 0.70:
        raise ValueError('按当前速度需运行 {:.1f} 小时，超过可安排的每日预算；请降低额度或提高速度'.format(active_seconds / 3600))
    return config


def load_config(path):
    return validate(json.loads(path.read_text(encoding='utf-8')))


@contextlib.contextmanager
def locked(directory):
    import fcntl
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'run.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop('已有任务正在运行，请稍后再试')
        yield


def new_day(day):
    return {'date': day, 'upload': {'bytes': 0, 'confirmed': 0, 'reserved': 0},
            'download': {'bytes': 0, 'confirmed': 0, 'reserved': 0},
            'test_upload': 0, 'test_download': 0, 'plan': None, 'last_tick': None}


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


def make_plan(config, data, moment, rng=None):
    rng = rng or random.SystemRandom()
    minute = moment.hour * 60 + moment.minute
    available = list(range(minute, 1440))
    # About 40 productive seconds per chosen minute, leaving DNS/TLS/retry headroom.
    sessions = min(len(available), math.ceil(remaining_seconds(config, data) / 40))
    chosen = sorted(rng.sample(available, sessions))
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    return {'minutes': chosen, 'jitter': rng.randrange(0, 5), 'config_hash': fingerprint}


def due(config, data, moment):
    if remaining_seconds(config, data) <= 0:
        return False
    minute = moment.hour * 60 + moment.minute
    # If retries/slow sources reduce headroom, use every remaining minute to catch up.
    pressure = remaining_seconds(config, data) > (1440 - minute) * 36
    return minute in data['plan']['minutes'] or pressure


def extend_plan(config, data, moment, rng=None):
    """Add random future slices when measured throughput needs more working time."""
    rng = rng or random.SystemRandom()
    minute = moment.hour * 60 + moment.minute
    planned = set(data['plan']['minutes'])
    future = set(range(minute, 1440))
    needed = min(len(future), math.ceil(remaining_seconds(config, data) / 40))
    extra = needed - len(planned & future)
    if extra > 0:
        planned.update(rng.sample(sorted(future - planned), extra))
        data['plan']['minutes'] = sorted(planned)
        return True
    return False


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


def transfer(network, direction, item, target, amount, directory, data, path, pacer):
    entry = data[direction]
    entry['reserved'] = amount
    atomic_json(path, data)
    actual = 0
    confirmed = False
    started = time.monotonic()
    conn = response = None
    url = download_url(target, amount) if direction == 'download' else target['url']
    try:
        for _ in range(5):
            conn, p, address, country = network.connect(url, item['region'])
            headers = {'User-Agent': UA, 'Accept-Encoding': 'identity', 'Connection': 'close'}
            if direction == 'upload':
                headers.update({'Content-Type': 'application/octet-stream', 'Content-Length': str(amount)})
                conn.putrequest('POST', request_path(p))
                for key, value in headers.items():
                    conn.putheader(key, value)
                conn.endheaders()
                random_block = os.urandom(BLOCK)
                while actual < amount:
                    size = min(BLOCK, amount - actual)
                    pacer.wait(size)
                    actual += size  # Conservative if sendall fails after a partial socket write.
                    conn.send(random_block[:size])
                response = conn.getresponse()
                if response.status not in (200, 201, 204):
                    raise OSError('上传被拒绝 HTTP {}（已发送仍计入额度）'.format(response.status))
                body = response.read(8193)
                if len(body) > 8192 or 'text/html' in response.getheader('Content-Type', '').lower():
                    raise OSError('上传端返回网页，未验证为接收端')
                if target['kind'] == 'receiver':
                    if json.loads(body).get('received_bytes') != actual:
                        raise OSError('接收端确认字节数不符')
                elif re.fullmatch(rb'\s*size=\d+\s*', body):
                    if int(body.strip().split(b'=')[1]) != actual:
                        raise OSError('测速接收端确认字节数不符')
                confirmed = True
                break
            headers['Range'] = 'bytes=0-{}'.format(amount - 1)
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
                raise OSError('下载 HTTP {}'.format(response.status))
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
                if first != 0 or not 0 <= last < total or last + 1 > amount:
                    raise OSError('Range 响应越界')
                expected = last + 1
            elif target['kind'] == 'range':
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
                    output.write(chunk)
                    pacer.wait(len(chunk))
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


def pick_direction(config, data):
    choices = [d for d in DIRECTIONS if config['directions'][d]['enabled']
               and data[d]['bytes'] < config['directions'][d]['daily_bytes']]
    return min(choices, key=lambda d: data[d]['bytes'] / config['directions'][d]['daily_bytes']) if choices else None


def run_session(config, data, path, directory, network):
    selected = {}
    failed = {d: set() for d in DIRECTIONS}
    pacers = {d: Pacer(config['directions'][d]['mbps']) for d in DIRECTIONS}
    while True:
        direction = pick_direction(config, data)
        if direction is None:
            return True
        item = config['directions'][direction]
        available = [t for t in targets(config, direction) if t['url'] not in failed[direction]]
        if not available:
            raise OSError('{}所有目标均失败；请执行 test 查看详情或重新配置'.format(LABEL[direction]))
        target = selected.get(direction, available[0])
        if target not in available:
            target = available[0]
        # At a low configured speed, finish a request inside a short time slice.
        amount = min(PIECE, max(1, int(item['mbps'] * 125000 * 8)),
                     item['daily_bytes'] - data[direction]['bytes'])
        before = data[direction]['confirmed']
        try:
            transfer(network, direction, item, target, amount, directory, data, path, pacers[direction])
            selected[direction] = target
            if data[direction]['confirmed'] <= before:
                raise OSError('没有实际确认的数据传输')
        except ERRORS as error:
            log('{}目标失败 {}: {}'.format(LABEL[direction], target['name'], error))
            failed[direction].add(target['url'])
            selected.pop(direction, None)


def load_geo(directory):
    path = directory / 'geo.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def tick(config, directory):
    with locked(directory):
        verified = json.loads((directory / 'last-test.json').read_text(encoding='utf-8'))
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        if verified.get('config_hash') != fingerprint:
            raise ValueError('当前配置尚未实测，请先运行 test')
        moment = now()
        path = directory / ('quota-' + moment.date().isoformat() + '.json')
        data = read_day(path, moment.date().isoformat())
        recover(data)
        if not data.get('plan') or data['plan'].get('config_hash') != fingerprint:
            data['plan'] = make_plan(config, data, moment)
            atomic_json(path, data)
            log('今日随机计划生成：{} 个分钟时段'.format(len(data['plan']['minutes'])))
            if remaining_seconds(config, data) > (1440 - moment.hour * 60 - moment.minute) * 36:
                log('今日剩余时间可能不足以刷足；将尽力传输，午夜按新日期计量，不虚报完成')
        elif extend_plan(config, data, moment):
            atomic_json(path, data)
        key = moment.hour * 60 + moment.minute
        if data.get('last_tick') == key or not due(config, data, moment):
            return 0
        data['last_tick'] = key
        atomic_json(path, data)
        until_midnight = (moment.replace(hour=0, minute=0, second=0, microsecond=0)
                          + dt.timedelta(days=1) - now()).total_seconds()
        seconds = min(52 - now().second - now().microsecond / 1e6, until_midnight)
        if seconds < 3:
            return 0
        network = Network(load_geo(directory))
        try:
            with deadline(seconds):
                jitter = min(data['plan']['jitter'], max(0, seconds - 3))
                time.sleep(jitter)
                run_session(config, data, path, directory, network)
                log('今日配置的计量额度已用完，已确认接收量可通过 status 查看')
        except Stop:
            log('本次随机时段结束，上行={} GB，下行={} GB'.format(
                round(data['upload']['bytes'] / 1e9, 4), round(data['download']['bytes'] / 1e9, 4)))
        finally:
            atomic_json(directory / 'geo.json', network.cache)
        return 0


def real_test(config, directory, megabytes=10):
    # Independent test ledger: never resets or spends the scheduled quota silently.
    directory.mkdir(parents=True, exist_ok=True)
    size = int(megabytes * 1_000_000)
    test_config = json.loads(json.dumps(config))
    download = test_config['directions']['download']
    if not download['enabled']:
        download['region'] = test_config['directions']['upload']['region']
        download['targets'] = []
    download['enabled'] = True  # A real download is mandatory even for upload-only setup.
    for d in DIRECTIONS:
        test_config['directions'][d]['daily_bytes'] = size
    network = Network(load_geo(directory))
    with tempfile.TemporaryDirectory(prefix='test-', dir=str(directory)) as temporary:
        test_dir = Path(temporary)
        data = new_day(now().date().isoformat())
        start = time.monotonic()
        try:
            with deadline(max(180, remaining_seconds(test_config, data) * 4 + 60)):
                run_session(test_config, data, test_dir / 'test.json', test_dir, network)
        finally:
            atomic_json(directory / 'geo.json', network.cache)
        for d in DIRECTIONS:
            if test_config['directions'][d]['enabled'] and data[d]['confirmed'] < size:
                raise OSError('{}实测未完成：确认 {} / {} 字节'.format(LABEL[d], data[d]['confirmed'], size))
        log('实测通过：下行={} MB，上行={} MB，耗时 {:.1f} 秒；测试流量单独计量'.format(
            data['download']['confirmed'] / 1e6, data['upload']['confirmed'] / 1e6, time.monotonic() - start))
        atomic_json(directory / 'last-test.json', {'at': now().isoformat(), 'version': VERSION,
                    'upload': data['upload'], 'download': data['download'],
                    'config_hash': hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()})


def prompt(text, default):
    answer = input('{} [{}]: '.format(text, default)).strip()
    return answer or str(default)


def wizard(path):
    config = load_config(path) if path.exists() else default_config()
    print('\n流量调度设置（北京时间，全天随机分散；GB=10^9 字节，Mbps=兆比特/秒）')
    print('上行=服务器向目标上传；下行=服务器从目标下载。双向按各自额度交替执行。')
    choice = prompt('模式：1 上行 / 2 下行 / 3 双向', '1')
    if choice not in ('1', '2', '3'):
        raise ValueError('模式只能选 1、2、3')
    for direction in DIRECTIONS:
        item = config['directions'][direction]
        item['enabled'] = choice == '3' or choice == ('1' if direction == 'upload' else '2')
        if not item['enabled']:
            continue
        print('\n设置' + LABEL[direction])
        quota = float(prompt('每日流量 GB', item['daily_bytes'] / 1e9))
        if not math.isfinite(quota) or quota <= 0:
            raise ValueError('每日流量必须是有限正数')
        item['daily_bytes'] = int(quota * 1e9)
        item['mbps'] = float(prompt('速度上限 Mbps', item['mbps']))
        region = prompt('目标：1 中国大陆 / 2 海外（含港澳台）', '2' if item['region'] == 'overseas' else '1')
        if region not in ('1', '2'):
            raise ValueError('目标只能选 1 或 2')
        item['region'] = 'cn' if region == '1' else 'overseas'
        item['targets'] = []
        custom = prompt('自定义 HTTP(S) 地址（直接回车使用内置）', '').strip()
        if custom:
            parse_url(custom)
            item['targets'] = [{'name': '自定义目标', 'url': custom,
                               'kind': 'post' if direction == 'upload' else 'stream'}]
        if not targets(config, direction):
            raise ValueError('当前区域暂无已验证的公共上传端。请填写你有权使用的接收 URL；不会切换到其他区域冒充成功。')
    validate(config)
    atomic_json(path, config)
    if os.name == 'posix' and os.geteuid() == 0:
        import grp
        try:
            os.chown(str(path), 0, grp.getgrnam('traffic-scheduler').gr_gid)
            os.chmod(str(path), 0o640)
        except KeyError:
            pass
    print('\n配置已保存：' + str(path))
    print('接下来必须进行真实下载/上传测试，测试通过后安装器才会启用定时任务。')


def status(config, directory):
    data = read_day(directory / ('quota-' + now().date().isoformat() + '.json'), now().date().isoformat())
    print('Traffic Scheduler {} | 北京时间 {}'.format(VERSION, now().isoformat(timespec='seconds')))
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
        print('下次计划时段: ' + ('{:02d}:{:02d}'.format(future[0] // 60, future[0] % 60) if future else '今日无剩余随机时段，必要时自动补足'))
    print('额度按有效负载计量；协议开销、重传、首次/手动实测另计。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=VERSION)
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('--state-dir', type=Path, default=STATE)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('configure', help='交互设置；修改后需要 test 再启动定时任务')
    commands.add_parser('tick', help='执行当前随机时间片')
    commands.add_parser('status', help='显示今日上下行进度')
    test = commands.add_parser('test', help='任何时间真实传输，默认每方向 10MB，且一定测下行')
    test.add_argument('--mb', type=float, default=10)
    args = parser.parse_args()
    if args.command == 'configure':
        wizard(args.config)
        return 0
    config = load_config(args.config)
    if args.command == 'status':
        status(config, args.state_dir)
        return 0
    if args.command == 'test':
        if not math.isfinite(args.mb) or not 0.001 <= args.mb <= 1000:
            raise ValueError('测试量必须在 0.001–1000 MB 之间')
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
TRAFFIC_SCHEDULER_PY
chmod 0755 /usr/local/bin/traffic-scheduler
trap 'echo "安装未完成：定时任务尚未启用。查看上方错误；修复网络或目标后重新执行本脚本。" >&2' ERR
if [[ $defaults -eq 1 ]]; then
    if [[ ! -f /etc/traffic-scheduler/config.json ]]; then
        /usr/bin/python3 - <<'TRAFFIC_DEFAULTS'
import runpy
m = runpy.run_path('/usr/local/bin/traffic-scheduler')
m['atomic_json'](m['CONFIG'], m['default_config']())
TRAFFIC_DEFAULTS
    fi
else
    /usr/bin/python3 /usr/local/bin/traffic-scheduler configure
fi
chown root:traffic-scheduler /etc/traffic-scheduler/config.json
chmod 0640 /etc/traffic-scheduler/config.json
cat > /etc/systemd/system/traffic-scheduler.service <<'TRAFFIC_SERVICE'
[Unit]
Description=Random daily upload and download traffic budget
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=traffic-scheduler
Group=traffic-scheduler
ExecStart=/usr/bin/python3 -u /usr/local/bin/traffic-scheduler tick
RuntimeMaxSec=57s
TimeoutStopSec=5s
StateDirectory=traffic-scheduler
StateDirectoryMode=0700
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
ReadWritePaths=/var/lib/traffic-scheduler
StandardOutput=journal
StandardError=journal
TRAFFIC_SERVICE
cat > /etc/systemd/system/traffic-scheduler.timer <<'TRAFFIC_TIMER'
[Unit]
Description=Check randomly selected transfer slices every minute

[Timer]
OnCalendar=*-*-* *:*:00 Asia/Shanghai
AccuracySec=1s
RandomizedDelaySec=0
Persistent=false
Unit=traffic-scheduler.service

[Install]
WantedBy=timers.target
TRAFFIC_TIMER
/usr/bin/python3 /usr/local/bin/traffic-scheduler status
systemd-analyze calendar '*-*-* *:*:00 Asia/Shanghai' >/dev/null
systemd-analyze verify /etc/systemd/system/traffic-scheduler.service /etc/systemd/system/traffic-scheduler.timer
systemctl daemon-reload
echo
echo '开始真实传输测试：必测下行 10MB，并测试已启用的上行 10MB；保留设置中的限速和地区校验。'
runuser -u traffic-scheduler -- /usr/bin/python3 -u /usr/local/bin/traffic-scheduler test --mb 10
systemctl enable --now traffic-scheduler.timer
trap - ERR
echo
echo '安装并实测通过，已启用全天随机分散任务。每天按北京时间重新计量。'
echo '状态: sudo traffic-scheduler status'
echo '随时实测: sudo -u traffic-scheduler traffic-scheduler test --mb 10'
echo '日志: sudo journalctl -u traffic-scheduler.service -f'
echo '修改设置: 再运行 sudo bash install.sh（不会清零当天额度）'
echo '停用: sudo systemctl disable --now traffic-scheduler.timer; sudo systemctl stop traffic-scheduler.service'
