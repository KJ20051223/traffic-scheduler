import contextlib
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from vps_toolkit import downloader, traffic
from vps_toolkit.cli import main
from vps_toolkit.core import Context, Error, defaults, read_json, save_config
from tests.test_toolkit import LocalNetwork


class Handler(BaseHTTPRequestHandler):
    payload = b'x' * 4_000_000
    active = maximum = 0
    ranges = []
    lock = threading.Lock()
    fail_once = False
    def log_message(self, *args):
        pass
    def do_GET(self):
        start, end = map(int, self.headers['Range'][6:].split('-'))
        with self.lock:
            type(self).active += 1
            type(self).maximum = max(self.maximum, self.active)
            self.ranges.append((start, end))
            truncated = self.path == '/retry' and self.fail_once
            if truncated:
                type(self).fail_once = False
        try:
            streaming = self.path == '/stream'
            body = self.payload if streaming else self.payload[start:end + 1]
            self.send_response(200 if streaming else 206)
            self.send_header('Content-Length', str(len(body)))
            if not streaming:
                self.send_header('Content-Range', 'bytes {}-{}/{}'.format(start, end, len(self.payload)))
            self.end_headers()
            if truncated:
                self.wfile.write(body[:4096])
                return
            for position in range(0, len(body), 4096):
                self.wfile.write(body[position:position + 4096])
                self.wfile.flush()
                time.sleep(0.003)
        except (ConnectionError, OSError):
            pass
        finally:
            with self.lock:
                type(self).active -= 1


class DownloaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = Context(self.temp.name)
        self.ctx.require_root = lambda: None
        self.cfg = defaults()
        self.cfg['traffic'].update(authorized_urls=['http://example.com/file', 'http://example.com/stream',
                                                  'http://example.com/retry'], mbps=8, max_runtime=20)
        save_config(self.ctx, self.cfg)
        self.at = dt.datetime(2026, 11, 6, 12, tzinfo=dt.timezone.utc)
        if os.name != 'posix':
            lock = patch.object(traffic.engine, 'locked', side_effect=lambda _: contextlib.nullcontext())
            lock.start()
            self.addCleanup(lock.stop)
        with Handler.lock:
            Handler.maximum = Handler.active = 0
            Handler.ranges = []
            Handler.fail_once = False
    def download(self, **kwargs):
        return traffic.download(self.ctx, network=LocalNetwork(self.server), at=self.at, **kwargs)
    def ledger(self):
        return read_json(self.ctx.path('/var/lib/traffic-scheduler/quota-2026-11-06.json'))['download']

    def test_real_parallel_ranges_share_exact_daily_budget_and_clean_payloads(self):
        self.cfg['traffic'].update(daily_bytes=300_017, monthly_bytes=300_017)
        save_config(self.ctx, self.cfg)
        started = time.monotonic()
        job = self.download(size=500_000)
        elapsed = time.monotonic() - started
        self.assertEqual(job['status'], 'budget_exhausted')
        self.assertEqual(job['received_bytes'], 300_017)
        self.assertGreaterEqual(Handler.maximum, 2)
        self.assertEqual(sum(last - first + 1 for first, last in Handler.ranges), 300_017)
        self.assertEqual(self.ledger()['bytes'], 300_017)
        self.assertEqual(self.ledger()['reserved'], 0)
        self.assertGreaterEqual(elapsed, 300_017 / 1_000_000 * 0.95)
        self.assertFalse(list(self.ctx.path('/var/lib/traffic-scheduler').glob('payload-*')))

    def test_speedtest_accepts_200_without_range_and_never_uploads_or_stores_data(self):
        self.cfg['traffic']['download_mode'] = 'speedtest'
        save_config(self.ctx, self.cfg)
        job = self.download(size=200_003, url='http://example.com/stream')
        self.assertEqual((job['status'], job['received_bytes']), ('completed', 200_003))
        self.assertEqual(len(Handler.ranges), 4)
        self.assertTrue(all(first == 0 for first, _ in Handler.ranges))
        self.assertEqual(self.ledger()['bytes'], 200_003)
        self.assertFalse(list(self.ctx.path('/var/lib/traffic-scheduler').glob('payload-*')))

    def test_file_range_ignored_fails_with_counted_partial_bytes(self):
        with self.assertRaisesRegex(OSError, 'Range'):
            self.download(size=200_000, url='http://example.com/stream')
        self.assertEqual(self.ledger()['reserved'], 0)
        self.assertLessEqual(self.ledger()['bytes'], 200_000)

    def test_truncated_range_retries_at_its_exact_missing_offset(self):
        Handler.fail_once = True
        job = self.download(size=200_000, url='http://example.com/retry')
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['received_bytes'], 200_000)
        self.assertEqual(self.ledger()['confirmed'], 200_000)
        self.assertTrue(any(first % 50_000 == 4096 for first, _ in Handler.ranges))

    def test_live_rate_decrease_and_increase_take_effect_inside_same_batch(self):
        self.cfg['traffic']['mbps'] = 2
        save_config(self.ctx, self.cfg)
        observed, edits, failures = [], [], []
        original = downloader.RateGate.acquire
        def acquire(gate, maximum):
            amount = original(gate, maximum)
            observed.append((time.monotonic(), gate.settings()['rate'], amount))
            return amount
        def editor():
            try:
                time.sleep(0.5)
                for speed in (0.5, 4):
                    config = read_json(self.ctx.config_path)
                    config['traffic']['mbps'] = speed
                    save_config(self.ctx, config)
                    edits.append((time.monotonic(), speed))
                    time.sleep(0.7)
            except BaseException as exc:
                failures.append(exc)
        editor_thread = threading.Thread(target=editor)
        editor_thread.start()
        try:
            with patch.object(downloader.RateGate, 'acquire', acquire):
                job = self.download(size=600_000)
        finally:
            editor_thread.join()
        self.assertFalse(failures)
        self.assertEqual(job['status'], 'completed')
        self.assertTrue(all(any(speed == row[1] for row in observed) for speed in (2, 0.5, 4)))
        for stamp, speed in edits:
            stable = [row for row in observed if stamp + 0.25 <= row[0] < stamp + 0.65]
            self.assertTrue(stable)
            self.assertTrue(all(row[1] == speed for row in stable))
            # All four connections together, allowing a single 50 ms bucket at
            # the observation boundary, rather than four independent ceilings.
            self.assertLessEqual(sum(row[2] for row in stable), speed * 125000 * 0.45 + 16384)
        self.assertEqual(len(Handler.ranges), 4)

    def test_pause_parallel_then_resume_fills_holes_without_redownloading_completed_ranges(self):
        self.cfg['traffic']['mbps'] = 2
        save_config(self.ctx, self.cfg)
        def pause_later():
            time.sleep(0.35)
            traffic.set_control(self.ctx, 'pause')
        editor = threading.Thread(target=pause_later)
        editor.start()
        try:
            job = self.download(size=400_000)
        finally:
            editor.join()
        self.assertEqual(job['status'], 'paused')
        self.assertGreater(job['received_bytes'], 0)
        self.assertTrue(job['pending_ranges'])
        pending = job['pending_ranges']
        self.assertEqual(sum(length for _, length in pending) + job['received_bytes'], 400_000)
        Handler.ranges = []
        traffic.set_control(self.ctx, 'resume')
        self.cfg['traffic'].update(mbps=8, max_concurrency=1)
        save_config(self.ctx, self.cfg)
        job = self.download(job_id=job['id'])
        self.assertEqual((job['status'], job['received_bytes']), ('completed', 400_000))
        self.assertEqual(sorted(Handler.ranges), sorted((first, first + length - 1) for first, length in pending))
        self.assertEqual(self.ledger()['bytes'], 400_000)

    def test_legacy_session_uses_live_global_rate_and_new_parallel_downloader(self):
        config = traffic.engine.default_config()
        config['directions']['download'].update(mbps=0.1, daily_bytes=180_000,
            targets=[{'name': 'fixture', 'url': 'http://example.com/file', 'kind': 'range'}])
        item = self.cfg['traffic']
        directory, path, data, _ = traffic.ledger(self.ctx, item, self.at)
        def settings(context):
            config = read_json(self.ctx.config_path)
            config['traffic']['state_dir'] = str(directory)
            return config
        with patch.object(traffic.engine, 'toolkit_home', return_value=self.ctx.root), \
                patch('vps_toolkit.core.Context', return_value=self.ctx), \
                patch.object(traffic, 'configuration', side_effect=settings):
            traffic.engine.run_session(config, data, path, directory, LocalNetwork(self.server), max_bytes=180_000)
        self.assertEqual(data['download']['bytes'], 180_000)
        self.assertEqual(config['directions']['download']['mbps'], 8)
        self.assertGreaterEqual(Handler.maximum, 2)

    def test_wrapped_http_errors_preserve_source_retry_classification(self):
        for status, retry in ((403, False), (418, False), (429, True), (503, True)):
            error = downloader.TransferError(traffic.engine.DownloadHTTPError(status, '1.1.1.1', 'CN'))
            self.assertEqual(traffic.engine.transient(error), retry)
            self.assertEqual(error.status, status)

    def test_hash_file_uses_ordered_single_reader_and_speedtest_rejects_hash(self):
        import hashlib
        job = self.download(size=200_000, expected_sha256=hashlib.sha256(b'x' * 200_000).hexdigest())
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(len(Handler.ranges), 1)
        self.cfg['traffic']['download_mode'] = 'speedtest'
        save_config(self.ctx, self.cfg)
        with self.assertRaisesRegex(Error, 'SHA256'):
            self.download(size=100, expected_sha256='a' * 64)
        self.assertEqual(self.ledger()['bytes'], 200_000)

    def test_cli_can_set_parallel_speedtest_without_changing_ratio_or_enabled_state(self):
        self.cfg['traffic'].update(strategy='ratio', enabled=True, paused=True)
        save_config(self.ctx, self.cfg)
        with patch('sys.stdout', io.StringIO()), patch.object(traffic, 'initialize_meter', return_value={}):
            self.assertEqual(main(['traffic', 'configure', '--download-mode', 'speedtest', '--max-concurrency',
                                   '4', '--mbps', '3'], self.ctx), 0)
        saved = read_json(self.ctx.config_path)['traffic']
        self.assertEqual((saved['strategy'], saved['enabled'], saved['paused']), ('ratio', True, True))
        self.assertEqual((saved['download_mode'], saved['max_concurrency'], saved['mbps']), ('speedtest', 4, 3))


class GateTests(unittest.TestCase):
    def test_four_threads_share_one_ceiling_with_no_startup_or_idle_burst(self):
        stamp = [0.0]
        observed = []
        gate = downloader.RateGate(lambda: {'rate': 1}, clock=lambda: stamp[0],
                                   sleep=lambda seconds: stamp.__setitem__(0, stamp[0] + seconds))
        def worker():
            left = 50_000
            while left:
                amount = gate.acquire(left)
                left -= amount
                observed.append((stamp[0], amount))
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertGreaterEqual(stamp[0], 200_000 / 125000)
        self.assertEqual(sum(amount for _, amount in observed), 200_000)
        self.assertGreaterEqual(min(stamp for stamp, _ in observed), 0.025)
        stamp[0] += 100
        before = stamp[0]
        self.assertLessEqual(gate.acquire(100_000), 3125)
        self.assertLessEqual(stamp[0] - before, 0.051)

    def test_lowered_ceiling_discards_old_credit(self):
        stamp, speed = [0.0], [100]
        gate = downloader.RateGate(lambda: {'rate': speed[0]}, clock=lambda: stamp[0],
                                   sleep=lambda seconds: stamp.__setitem__(0, stamp[0] + seconds))
        gate.acquire(16384)
        stamp[0] += 100
        speed[0] = 0.1
        before = stamp[0]
        size = gate.acquire(16384)
        self.assertLessEqual(size, 312)
        self.assertGreaterEqual(stamp[0] - before, size / 12500 * 0.99)


if __name__ == '__main__':
    unittest.main()
