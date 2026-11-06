"""Bounded HTTP download workers with one live, aggregate payload rate gate.

Only the coordinator writes the quota ledger. All workers share its reservation;
unfinished file ranges survive pause/retry without treating a byte count as an
offset into a partially completed parallel batch.
"""
import math
import random
import re
import socket
import tempfile
import threading
import time
from urllib.parse import urljoin
from compat import traffic_scheduler as engine
from .core import Error, configuration, read_json, write_json


def batch_bytes(rate, connections, seconds=8):
    cap = engine.PIECE if connections == 1 else 256 * 1024 * 1024
    return min(cap, max(1, int(rate * 125000 * seconds)))


def cooldown_until(directory):
    record = read_json(directory / 'cooldown.json', {})
    if not isinstance(record, dict):
        raise Error('下载间隔记录无效，拒绝跳过等待')
    value = record.get('until', 0)
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise Error('下载间隔记录无效，拒绝跳过等待')
    return value


class TransferError(OSError):
    attempts_exhausted = True
    def __init__(self, cause):
        super().__init__('并行下载失败（已计入实际接收量）: ' + str(cause))
        self.original = cause
        self.status = getattr(cause, 'status', None)
        self.retry_after = getattr(cause, 'retry_after', 0)


class RateGate:
    """Pre-read token bucket; no startup credit and no per-connection allowance."""
    def __init__(self, settings, clock=time.monotonic, sleep=time.sleep):
        self.settings, self.clock, self.sleep = settings, clock, sleep
        self.lock = threading.RLock()
        self.tokens = 0.0
        self.previous_rate = None
        self.updated = clock()

    def acquire(self, maximum):
        with self.lock:
            while True:
                rate = self.settings()['rate'] * 125000
                stamp = self.clock()
                # A changed ceiling never inherits credit accumulated at an old rate.
                if self.previous_rate != rate:
                    self.tokens = 0.0
                else:
                    self.tokens = min(max(1, rate * 0.05), self.tokens + max(0, stamp - self.updated) * rate)
                self.previous_rate, self.updated = rate, stamp
                amount = min(maximum, 16384, max(1, int(rate * 0.025)))
                if self.tokens >= amount:
                    self.tokens -= amount
                    return amount
                self.sleep(min(0.05, max(0.001, (amount - self.tokens) / rate)))


class Session:
    def __init__(self, ctx, mbps=None, legacy=False, pending=None):
        from .traffic import effective_rate, validate
        self.ctx, self.ceiling, self.legacy = ctx, mbps, legacy
        self.initial = validate(configuration(ctx)['traffic'])
        self.mode = self.initial.get('download_mode', 'file')
        self.pending = pending or []
        if any(not isinstance(r, list) or len(r) != 2 or any(type(n) is not int for n in r)
               or r[0] < 0 or r[1] <= 0 for r in self.pending):
            raise Error('任务未完成分段记录无效')
        self.cached = None
        self.checked = -math.inf
        self.settings_lock = threading.RLock()
        self.cancel = threading.Event()
        self.gate = RateGate(self.settings)
        self.connections = 1
        self.active_connections = 0
        self.rate = effective_rate(self.initial)
        self.identifier = None
        self.target_url = None
        self.sockets = set()
        self.socket_lock = threading.Lock()

    def batch_bytes(self):
        settings = self.settings()
        return batch_bytes(settings['rate'], settings['max_concurrency'], settings['batch_seconds'])

    def wait_ready(self, directory, progress):
        # Previous failed workers have joined before the coordinator retries or
        # switches source. Their cancellation must not cancel the next batch.
        self.cancel.clear()
        self.checked = -math.inf
        until = cooldown_until(directory)
        if self.settings()['cooldown_max_seconds'] == 0:
            until = 0
        progress.connections = 0
        progress.cooldown_until = until
        if until > time.time():
            engine.log('任务间隔：随机等待剩余 {:.1f} 秒'.format(until - time.time()))
        while until > time.time():
            self.settings()  # Pause/stop and configuration errors remain responsive while idle.
            progress(0, 0)
            time.sleep(min(0.2, max(0, until - time.time())))
        progress.cooldown_until = 0

    def schedule_cooldown(self, directory):
        from .traffic import validate
        settings = validate(configuration(self.ctx)['traffic'])
        delay = random.SystemRandom().uniform(settings['cooldown_min_seconds'], settings['cooldown_max_seconds'])
        finished = time.time()
        write_json(directory / 'cooldown.json', {'finished_at': finished, 'delay_seconds': delay,
                                               'until': finished + delay})

    def abort(self):
        self.cancel.set()
        with self.socket_lock:
            for sock in self.sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def settings(self):
        from .traffic import effective_rate, task_directory, validate
        with self.settings_lock:
            if self.cancel.is_set():
                raise engine.Stop('下载已取消')
            stamp = time.monotonic()
            if self.cached is None or stamp - self.checked >= 0.2:
                item = validate(configuration(self.ctx)['traffic'])
                if any(item[k] != self.initial[k] for k in ('state_dir', 'timezone', 'region')):
                    raise engine.Stop('计量目录、时区或地区已更改，请重新启动下载')
                flags = read_json(task_directory(self.ctx) / 'control.json', {})
                if item['paused'] or flags.get('pause') or self.identifier and flags.get('stop') == self.identifier:
                    raise engine.Stop('下载已暂停或停止')
                if self.target_url and self.target_url not in item['authorized_urls']:
                    raise engine.Stop('下载地址授权已撤销')
                self.rate = effective_rate(item)
                if self.ceiling is not None:
                    self.rate = min(self.rate, self.ceiling)
                self.cached = dict(item, rate=self.rate)
                self.checked = stamp
            return self.cached

    def _receive(self, network, target, offset, amount, on_chunk, hasher):
        """One bounded range. No worker may write a shared quota/progress file."""
        conn = response = raw = None
        url = engine.download_url(target, amount)
        try:
            for _ in range(5):
                self.settings()
                conn, parsed, address, country = network.connect(url, self.initial['region'])
                conn.request('GET', engine.request_path(parsed), headers={
                    'User-Agent': engine.UA, 'Accept-Encoding': 'identity', 'Connection': 'close',
                    'Range': 'bytes={}-{}'.format(offset, offset + amount - 1)})
                raw = conn.sock
                if raw is not None:
                    with self.socket_lock:
                        self.sockets.add(raw)
                    self.settings()
                response = conn.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if not location:
                        raise OSError('重定向缺少目标')
                    url = urljoin(url, location)
                    engine.parse_url(url)
                    response.close()
                    conn.close()
                    with self.socket_lock:
                        self.sockets.discard(raw)
                    continue
                if response.status not in (200, 206):
                    raise engine.DownloadHTTPError(response.status, address, country, response.getheader('Retry-After'))
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
                    raise OSError('资源不支持分段 Range；可改用下行测速模式独立读取')
                elif response.getheader('Content-Length'):
                    expected = min(amount, int(response.getheader('Content-Length')))
                if expected <= 0:
                    raise OSError('下载内容为空')
                actual = 0
                with tempfile.TemporaryFile(prefix='payload-', dir=str(self.ctx.path(self.initial['state_dir']))) as output:
                    while actual < expected:
                        permitted = self.gate.acquire(expected - actual)
                        chunk = response.read1(permitted)
                        if not chunk:
                            raise OSError('下载提前中断')
                        # Account before any fallible disk write or cancellation check.
                        actual += len(chunk)
                        on_chunk(len(chunk))
                        if hasher is not None:
                            hasher.update(chunk)
                        output.write(chunk)
                if actual != amount:
                    raise OSError('资源长度不足本次分段')
                return address, country
            raise OSError('下载重定向过多')
        finally:
            with self.socket_lock:
                self.sockets.discard(raw)
            if response is not None:
                response.close()
            if conn is not None:
                conn.close()

    def transfer(self, network, direction, item, target, amount, directory, data, path,
                 offset=0, hasher=None, progress=None):
        if direction != 'download':
            raise ValueError('仅支持下行')
        self.cancel.clear()
        self.checked = -math.inf
        if self.legacy:
            self.pending = []  # Legacy chunks measure traffic, not a resumable file.
        self.target_url = target['url']
        settings = self.settings()
        self.connections = min(settings['max_concurrency'], amount) if hasher is None else 1
        if hasher is not None and self.mode == 'speedtest':
            raise Error('测速数据不支持文件 SHA256 校验，请使用 file 模式')
        # Pending ranges identify holes, rather than assuming received_bytes is a
        # contiguous offset. The unused tail is retained when a budget shrinks.
        if self.pending:
            ranges, tail, left = [], [], amount
            for first, length in self.pending:
                take = min(length, left)
                if take:
                    ranges.append([first, take])
                if take < length:
                    tail.append([first + take, length - take])
                left -= take
            amount -= left
        else:
            ranges, tail = [], []
            each, extra = divmod(amount, self.connections)
            cursor = offset
            for index in range(self.connections):
                length = each + (index < extra)
                ranges.append([cursor, length])
                cursor += length
        received = [0] * len(ranges)
        completed = [False] * len(ranges)
        errors = []
        lock = threading.RLock()
        entry = data['download']
        if progress is None:
            from .progress import reporter
            progress = reporter(directory, data, requested=amount)
        progress.connections = self.connections
        progress.mode = self.mode
        self.wait_ready(directory, progress)
        progress.connections = self.connections
        started = time.monotonic()
        entry['reserved'] = amount
        engine.atomic_json(path, data)
        progress(0, 0)

        def worker(index):
            first, length = ranges[index]
            def add(count):
                with lock:
                    received[index] += count
            with lock:
                self.active_connections += 1
            try:
                for attempt in range(3):
                    try:
                        current = received[index]
                        # Speed tests are independent bounded responses, not file assembly.
                        position = 0 if self.mode == 'speedtest' or target['kind'] == 'generated' else first + current
                        selected = dict(target, kind='file') if self.mode == 'speedtest' and target['kind'] != 'generated' else target
                        address, country = self._receive(network, selected, position, length - current, add, hasher)
                        completed[index] = True
                        return
                    except engine.ERRORS as exc:
                        if received[index] == length:
                            completed[index] = True
                            return
                        # The legacy scheduler owns per-source Retry-After and
                        # failover; do not hide those signals inside a worker.
                        if self.legacy or attempt == 2 or not engine.transient(exc):
                            raise
                        until = time.monotonic() + max(attempt + 1, getattr(exc, 'retry_after', 0))
                        while time.monotonic() < until:
                            self.settings()
                            time.sleep(min(0.05, max(0, until - time.monotonic())))
            except BaseException as exc:
                with lock:
                    errors.append(exc)
                self.abort()
            finally:
                with lock:
                    self.active_connections -= 1

        pending_indices = iter(range(len(ranges)))
        def run_worker():
            while not self.cancel.is_set():
                with lock:
                    index = next(pending_indices, None)
                if index is None:
                    return
                worker(index)
        threads = [threading.Thread(target=run_worker, daemon=True)
                   for _ in range(min(self.connections, len(ranges)))]
        interrupted = None
        try:
            for thread in threads:
                thread.start()
            while any(thread.is_alive() for thread in threads):
                with lock:
                    actual = sum(received)
                progress.connections = self.active_connections
                progress(actual, time.monotonic() - started)
                time.sleep(0.05)
        except BaseException as exc:
            interrupted = exc
            self.abort()
        finally:
            # Stop readers and wait for socket timeouts before releasing the quota
            # lock. No worker may continue traffic after its reservation settles.
            for thread in threads:
                if thread.ident is not None:
                    thread.join()
            actual = sum(received)
            self.pending = [[first + received[index], length - received[index]]
                            for index, (first, length) in enumerate(ranges) if received[index] < length] + tail
            entry['bytes'] += actual
            entry['confirmed'] += sum(received[i] for i in range(len(ranges)) if completed[i])
            entry['reserved'] = 0
            elapsed = max(0.000001, time.monotonic() - started)
            if actual:
                entry['observed_bps'] = (entry.get('observed_bps', actual / elapsed) + actual / elapsed) / 2
            engine.atomic_json(path, data)
            progress(actual, elapsed, final=True)
        if interrupted:
            raise interrupted
        if errors:
            # Already retried exact unfinished ranges; never restart at sum(bytes).
            substantive = errors[0]
            if isinstance(substantive, engine.Stop):
                raise substantive
            if isinstance(substantive, engine.ERRORS):
                raise TransferError(substantive)
            raise substantive
        self.schedule_cooldown(directory)
        engine.log('下行 {} 字节，{} 连接，模式={}，临时内容已清理'.format(actual, self.connections, self.mode))
        return actual


def legacy_session():
    from .core import Context
    return Session(Context(), legacy=True)
