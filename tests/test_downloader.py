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
from vps_toolkit.core import Context, Error, defaults, read_json, save_config, write_json
from tests.test_toolkit import LocalNetwork


class Handler(BaseHTTPRequestHandler):
    total_size = 100_000_000
    delay = 0.003
    active = maximum = 0
    ranges = []
    starts = []
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
            self.starts.append((start, time.monotonic()))
            truncated = self.path == '/retry' and self.fail_once
            if truncated:
                type(self).fail_once = False
        try:
            if self.path == '/blocked':
                self.send_error(403)
                return
            streaming = self.path == '/stream'
            length = 4_000_000 if streaming else min(end + 1, self.total_size) - start
            self.send_response(200 if streaming else 206)
            self.send_header('Content-Length', str(length))
            if not streaming:
                self.send_header('Content-Range', 'bytes {}-{}/{}'.format(start, start + length - 1, self.total_size))
            self.end_headers()
            if truncated:
                self.wfile.write(b'x' * min(4096, length))
                return
            for position in range(0, length, 4096):
                self.wfile.write(b'x' * min(4096, length - position))
                self.wfile.flush()
                if self.delay:
                    time.sleep(self.delay)
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
                                                  'http://example.com/retry'], mbps=8, max_runtime=20,
                                   cooldown_min_seconds=0, cooldown_max_seconds=0)
        save_config(self.ctx, self.cfg)
        self.at = dt.datetime(2026, 11, 6, 12, tzinfo=dt.timezone.utc)
        if os.name != 'posix':
            lock = patch.object(traffic.engine, 'locked', side_effect=lambda _: contextlib.nullcontext())
            lock.start()
            self.addCleanup(lock.stop)
        with Handler.lock:
            Handler.maximum = Handler.active = 0
            Handler.ranges = []
            Handler.starts = []
            Handler.fail_once = False
            Handler.delay = 0.003
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

    def test_legacy_failover_still_works_after_failed_workers_are_cancelled(self):
        self.cfg['traffic']['authorized_urls'].append('http://example.com/blocked')
        save_config(self.ctx, self.cfg)
        pool = [{'name': 'blocked', 'url': 'http://example.com/blocked', 'kind': 'range'},
                {'name': 'working', 'url': 'http://example.com/file', 'kind': 'range'}]
        config = traffic.engine.default_config()
        config['directions']['download'].update(daily_bytes=180_000, targets=pool)
        directory, path, data, _ = traffic.ledger(self.ctx, self.cfg['traffic'], self.at)
        def settings(context):
            current = read_json(self.ctx.config_path)
            current['traffic']['state_dir'] = str(directory)
            return current
        health = {}
        with patch.object(traffic.engine, 'toolkit_home', return_value=self.ctx.root), \
                patch('vps_toolkit.core.Context', return_value=self.ctx), \
                patch.object(traffic, 'configuration', side_effect=settings), \
                patch.object(traffic.engine, 'shuffled_targets', return_value=pool):
            traffic.engine.run_session(config, data, path, directory, LocalNetwork(self.server),
                                       max_bytes=180_000, health=health)
        self.assertEqual(data['download']['bytes'], 180_000)
        self.assertEqual(health[pool[0]['url']]['status'], 403)

    def test_50mbps_download_uses_one_50mb_batch_with_four_large_ranges(self):
        self.cfg['traffic']['mbps'] = 50
        save_config(self.ctx, self.cfg)
        Handler.delay = 0
        with patch.object(downloader.RateGate, 'acquire', side_effect=lambda n: min(n, 32768)):
            job = self.download(size=50_000_000)
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['received_bytes'], 50_000_000)
        self.assertEqual(sorted(Handler.ranges), [(0, 12_499_999), (12_500_000, 24_999_999),
                                                  (25_000_000, 37_499_999), (37_500_000, 49_999_999)])
        self.assertEqual(self.ledger()['bytes'], 50_000_000)
        self.assertFalse(list(self.ctx.path('/var/lib/traffic-scheduler').glob('payload-*')))

    def test_new_session_honors_persisted_random_wait_without_resetting_it(self):
        self.cfg['traffic'].update(cooldown_min_seconds=5, cooldown_max_seconds=10)
        save_config(self.ctx, self.cfg)
        directory = self.ctx.path('/var/lib/traffic-scheduler')
        beginning = time.time()
        stamp = [beginning]
        updates = []
        def report(*args):
            updates.append((stamp[0], report.cooldown_until))
        def sleep(seconds):
            stamp[0] += seconds
        with patch.object(downloader.time, 'time', side_effect=lambda: stamp[0]), \
                patch.object(downloader.time, 'sleep', side_effect=sleep), \
                patch.object(downloader.random, 'SystemRandom') as rng:
            rng.return_value.uniform.return_value = 7.5
            downloader.Session(self.ctx).schedule_cooldown(directory)
            rng.return_value.uniform.assert_called_once_with(5, 10)
            saved = (directory / 'cooldown.json').read_bytes()
            downloader.Session(self.ctx).wait_ready(directory, report)
            self.assertAlmostEqual(stamp[0], beginning + 7.5)
            self.assertTrue(updates)
            self.assertTrue(all(until == beginning + 7.5 for _, until in updates))
            self.assertEqual((directory / 'cooldown.json').read_bytes(), saved)

    def test_budget_rechecked_after_wait_before_opening_any_connection(self):
        self.cfg['traffic'].update(cooldown_min_seconds=0.15, cooldown_max_seconds=0.15)
        save_config(self.ctx, self.cfg)
        directory = self.ctx.path('/var/lib/traffic-scheduler')
        write_json(directory / 'cooldown.json', {'until': time.time() + 1.5})
        def editor():
            time.sleep(0.05)
            config = read_json(self.ctx.config_path)
            config['traffic']['daily_bytes'] = 17
            save_config(self.ctx, config)
        thread = threading.Thread(target=editor)
        thread.start()
        try:
            job = self.download(size=1000)
        finally:
            thread.join()
        self.assertEqual((job['status'], job['received_bytes']), ('budget_exhausted', 17))
        self.assertEqual(sum(last - first + 1 for first, last in Handler.ranges), 17)

    def test_multiple_large_batches_wait_between_connections_and_keep_exact_total(self):
        self.cfg['traffic'].update(cooldown_min_seconds=0.1, cooldown_max_seconds=0.1)
        save_config(self.ctx, self.cfg)
        Handler.delay = 0
        with patch.object(downloader.RateGate, 'acquire', side_effect=lambda n: min(n, 32768)):
            job = self.download(size=10_000_000)
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(self.ledger()['bytes'], 10_000_000)
        first = [stamp for position, stamp in Handler.starts if position < 8_000_000]
        second = [stamp for position, stamp in Handler.starts if position >= 8_000_000]
        self.assertEqual((len(first), len(second)), (4, 4))
        self.assertGreaterEqual(min(second) - max(first), 0.095)
        self.assertEqual(sum(end - start + 1 for start, end in Handler.ranges), 10_000_000)

    def test_pause_during_random_wait_stops_without_network_or_quota_reservation(self):
        self.cfg['traffic'].update(cooldown_min_seconds=5, cooldown_max_seconds=10)
        save_config(self.ctx, self.cfg)
        directory = self.ctx.path('/var/lib/traffic-scheduler')
        write_json(directory / 'cooldown.json', {'until': time.time() + 7})
        def editor():
            time.sleep(0.15)
            traffic.set_control(self.ctx, 'pause')
        thread = threading.Thread(target=editor)
        thread.start()
        try:
            job = self.download(size=1000)
        finally:
            thread.join()
        self.assertEqual(job['status'], 'paused')
        self.assertEqual(Handler.ranges, [])
        self.assertEqual(job['received_bytes'], 0)

    def test_batch_configuration_validation_never_overwrites_invalid_values(self):
        before = self.ctx.config_path.read_bytes()
        for flags in (['--batch-seconds', '0'], ['--batch-seconds', 'nan'],
                      ['--cooldown-min-seconds', '11', '--cooldown-max-seconds', '10']):
            with patch('sys.stderr', io.StringIO()):
                self.assertEqual(main(['traffic', 'configure'] + flags, self.ctx), 1)
            self.assertEqual(self.ctx.config_path.read_bytes(), before)

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
    def test_batch_sizes_follow_aggregate_speed_and_cap_total_reservation(self):
        self.assertEqual(downloader.batch_bytes(50, 4), 50_000_000)
        self.assertEqual(downloader.batch_bytes(5, 4), 5_000_000)
        self.assertEqual(downloader.batch_bytes(0.1, 4), 100_000)
        self.assertEqual(downloader.batch_bytes(50, 4, 12), 75_000_000)
        self.assertEqual(downloader.batch_bytes(50, 1), 2 * 1024 * 1024)
        self.assertEqual(downloader.batch_bytes(10000, 32), 256 * 1024 * 1024)
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
