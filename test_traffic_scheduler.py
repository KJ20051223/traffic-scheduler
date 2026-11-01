import contextlib
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import random
import tempfile
import threading
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('traffic', Path(__file__).with_name('traffic_scheduler.py'))
t = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(t)


class NoPacer:
    def __init__(self, *args):
        pass

    def wait(self, size):
        pass


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    payloads = []
    requests = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.requests[self.path] = self.requests.get(self.path, 0) + 1
        if self.path in ('/always503', '/always403', '/always418', '/retry-after') or (
                self.path == '/flaky' and self.requests[self.path] == 1):
            status = {'/always403': 403, '/always418': 418, '/retry-after': 429}.get(self.path, 503)
            self.send_response(status)
            self.send_header('Content-Length', '0')
            self.send_header('Connection', 'close')
            if self.path == '/retry-after':
                self.send_header('Retry-After', '120')
            self.end_headers()
            return
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/file')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        requested = self.headers.get('Range', 'bytes=0-0')
        amount = int(requested.split('-')[1]) + 1
        if self.path == '/blocked':
            body, status, kind = b'<html>denied</html>', 200, 'text/html'
        elif self.path == '/truncated':
            body, status, kind = b'x' * 5, 206, 'application/octet-stream'
        elif self.path == '/ignored':
            body, status, kind = b'x' * 100, 200, 'application/octet-stream'
        else:
            body, status, kind = b'x' * amount, 206, 'application/octet-stream'
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        if status == 206:
            self.send_header('Content-Range', 'bytes 0-{}/1000000'.format(amount - 1))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)



class LocalNetwork:
    def __init__(self, address):
        self.address = address
        self.calls = []

    def connect(self, url, region):
        self.calls.append((url, region))
        p = t.parse_url(url)
        conn = t.http.client.HTTPConnection(*self.address, timeout=2)
        return conn, p, '180.208.59.230', 'CN'


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=2)

    def setUp(self):
        test_root = Path(__file__).resolve().parent
        self.temporary = tempfile.TemporaryDirectory(dir=str(test_root))
        self.directory = Path(self.temporary.name).resolve()
        self.assertEqual(self.directory.parent, test_root)
        self.addCleanup(self.temporary.cleanup)
        self.path = self.directory / 'quota.json'
        self.data = t.new_day('2026-11-01')
        self.config = t.default_config()
        self.net = LocalNetwork(self.server.server_address)
        self.item = {'region': 'cn', 'mbps': 10}
        FixtureHandler.requests = {}

    def target(self, path, kind='range'):
        return {'name': 'fixture', 'url': 'http://example.com' + path, 'kind': kind}

    def transfer(self, direction, path='/file', amount=37001, kind='range'):
        return t.transfer(self.net, direction, self.item, self.target(path, kind), amount,
                          self.directory, self.data, self.path, NoPacer())


    def test_real_http_download_exact_count_and_cleanup(self):
        self.assertEqual(self.transfer('download'), 37001)
        self.assertEqual(self.data['download']['bytes'], 37001)
        self.assertEqual(self.data['download']['confirmed'], 37001)
        self.assertEqual([p.name for p in self.directory.iterdir()], ['quota.json'])





    def test_block_page_not_downloaded_or_counted(self):
        with self.assertRaises(OSError):
            self.transfer('download', '/blocked', 17)
        self.assertEqual(self.data['download']['bytes'], 0)

    def test_ignored_range_rejected_before_body(self):
        with self.assertRaises(OSError):
            self.transfer('download', '/ignored', 17)
        self.assertEqual(self.data['download']['bytes'], 0)

    def test_partial_failed_download_still_counts_payload(self):
        with self.assertRaises(OSError):
            self.transfer('download', '/truncated', 17)
        self.assertEqual(self.data['download']['bytes'], 5)
        self.assertEqual(self.data['download']['reserved'], 0)
        self.assertFalse(list(self.directory.glob('payload-*')))

    def test_redirect_destination_rechecked(self):
        self.transfer('download', '/redirect', 17)
        self.assertEqual(len(self.net.calls), 2)
        self.assertEqual(self.net.calls[1], ('http://example.com/file', 'cn'))

    def test_interruption_cleans_and_settles_read_bytes(self):
        class Interrupted:
            def wait(self, size):
                raise t.Stop('interrupted')
        with self.assertRaises(t.Stop):
            t.transfer(self.net, 'download', self.item, self.target('/file'), 17,
                       self.directory, self.data, self.path, Interrupted())
        self.assertEqual(self.data['download']['bytes'], 17)
        self.assertEqual(self.data['download']['reserved'], 0)
        self.assertFalse(list(self.directory.glob('payload-*')))

    def test_crash_reservations_are_conservative(self):
        self.data['download'].update(bytes=3, reserved=7)
        t.atomic_json(self.path, self.data)
        restored = t.read_day(self.path, '2026-11-01')
        t.recover(restored)
        self.assertEqual(restored['download'], {'bytes': 10, 'confirmed': 0, 'reserved': 0})

    def test_corrupt_state_never_silently_resets(self):
        self.path.write_text('{bad json')
        with self.assertRaises(ValueError):
            t.read_day(self.path, '2026-11-01')

    def test_next_beijing_date_resets_daily_quota(self):
        self.data['download']['bytes'] = 123
        t.atomic_json(self.path, self.data)
        next_day = t.read_day(self.path, '2026-11-02')
        self.assertEqual(next_day['download']['bytes'], 0)

    def test_random_plan_spreads_across_whole_day(self):
        moment = dt.datetime(2026, 11, 1, tzinfo=t.BJ)
        first = t.make_plan(self.config, self.data, moment, random.Random(11))
        second = t.make_plan(self.config, self.data, moment, random.Random(12))
        self.assertNotEqual(first['minutes'], second['minutes'])
        self.assertEqual(len(first['minutes']), len(set(first['minutes'])))
        self.assertLess(min(first['minutes']), 120)
        self.assertGreater(max(first['minutes']), 1320)
        self.assertGreater(max(b - a for a, b in zip(first['minutes'], first['minutes'][1:])), 1)
        self.assertGreaterEqual(len(first['minutes']) * 40, t.remaining_seconds(self.config, self.data))

    def test_late_install_never_schedules_in_the_past(self):
        moment = dt.datetime(2026, 11, 1, 23, 55, tzinfo=t.BJ)
        plan = t.make_plan(self.config, self.data, moment)
        self.assertEqual(plan['minutes'], list(range(1435, 1440)))

    def test_behind_schedule_catches_up(self):
        self.data['plan'] = {'minutes': []}
        moment = dt.datetime(2026, 11, 1, 23, tzinfo=t.BJ)
        self.assertTrue(t.due(self.config, self.data, moment))
        self.data['download']['bytes'] = self.config['directions']['download']['daily_bytes']
        self.assertFalse(t.due(self.config, self.data, moment))

    def test_slow_measured_network_adds_random_future_slices(self):
        moment = dt.datetime(2026, 11, 1, 8, tzinfo=t.BJ)
        self.data['plan'] = t.make_plan(self.config, self.data, moment, random.Random(1))
        before = set(self.data['plan']['minutes'])
        self.data['download']['observed_bps'] = 312500
        self.assertEqual(t.remaining_seconds(self.config, self.data), 64000)
        self.assertTrue(t.extend_plan(self.config, self.data, moment, random.Random(2)))
        after = set(self.data['plan']['minutes'])
        self.assertTrue(before <= after)
        self.assertEqual(len(after), 960)
        self.assertTrue(all(n >= 480 for n in after))
        self.assertFalse(t.extend_plan(self.config, self.data, moment))

    def test_corrupt_observed_speed_fails_closed(self):
        self.data['download']['observed_bps'] = -10
        t.atomic_json(self.path, self.data)
        with self.assertRaises(ValueError):
            t.read_day(self.path, '2026-11-01')


    def test_source_failover(self):
        d = self.config['directions']['download']
        d.update(enabled=True, daily_bytes=31, targets=[self.target('/blocked'), self.target('/file')])
        with patch.object(t, 'Pacer', NoPacer):
            t.run_session(self.config, self.data, self.path, self.directory, self.net)
        self.assertEqual(self.data['download']['confirmed'], 31)

    def test_region_filter_never_falls_back_across_regions(self):
        net = t.Network()
        for country in ['US', 'HK', 'MO', 'TW']:
            with patch.object(net, 'resolve', return_value=['8.8.8.8']), \
                    patch.object(net, 'country', return_value=country), \
                    patch.object(t.socket, 'socket', side_effect=AssertionError('must not connect')):
                with self.assertRaises(OSError):
                    net.connect('https://example.com/file', 'cn')

    def test_dns_fake_ip_uses_doh_without_system_changes(self):
        net = t.Network()
        with patch.object(t.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('198.18.0.1', 443))]), \
                patch.object(t, 'json_get', return_value={'Answer': [{'type': 1, 'data': '8.8.8.8'}]}):
            self.assertEqual(net.resolve('example.com', 443), ['8.8.8.8'])

    def test_rate_pacer_and_no_accumulated_burst_credit(self):
        clock = [0.0]
        with patch.object(t.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(t.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            pacer = t.Pacer(5)
            pacer.wait(10_000_000)
            self.assertAlmostEqual(clock[0], 16)
            clock[0] = 100
            pacer.wait(1_250_000)
            self.assertAlmostEqual(clock[0], 102)

    def test_reject_impossible_daily_workload(self):
        self.config['directions']['download']['daily_bytes'] = 1_000_000_000_000
        with self.assertRaises(ValueError):
            t.validate(self.config)

    def test_nonfinite_speed_rejected(self):
        for value in [float('nan'), float('inf'), 0, -1, True]:
            self.config['directions']['download']['mbps'] = value
            with self.assertRaises(ValueError):
                t.validate(self.config)

    @unittest.skipUnless(os.name == 'posix', 'fcntl lock is Linux/POSIX only')
    def test_real_lock_excludes_overlapping_manual_test(self):
        with t.locked(self.directory):
            with self.assertRaises(t.Stop):
                with t.locked(self.directory):
                    self.fail('second process should be excluded')

    def test_tick_rejects_untested_changed_configuration(self):
        t.atomic_json(self.directory / 'last-test.json', {'config_hash': 'old-settings'})
        with patch.object(t, 'locked', return_value=contextlib.nullcontext()):
            with self.assertRaisesRegex(ValueError, '尚未实测'):
                t.tick(self.config, self.directory)

    def test_tick_runs_only_once_per_minute_and_preserves_quota(self):
        moment = dt.datetime(2026, 11, 1, 12, 0, 0, tzinfo=t.BJ)
        fingerprint = t.hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()
        t.atomic_json(self.directory / 'last-test.json', {'config_hash': fingerprint})
        self.data['download']['bytes'] = 123
        self.data['last_tick'] = 720
        self.data['plan'] = {'minutes': [720, 721], 'config_hash': fingerprint, 'jitter': 0}
        ledger = self.directory / 'quota-2026-11-01.json'
        t.atomic_json(ledger, self.data)
        with patch.object(t, 'locked', return_value=contextlib.nullcontext()), \
                patch.object(t, 'now', return_value=moment), \
                patch.object(t, 'run_session', side_effect=AssertionError('duplicate execution')):
            self.assertEqual(t.tick(self.config, self.directory), 0)
        self.assertEqual(t.read_day(ledger, '2026-11-01')['download']['bytes'], 123)







    def run_configure(self, *options):
        config_path = self.directory / 'config.json'
        with patch.object(t.sys, 'argv', ['traffic-scheduler', '--config', str(config_path), 'configure'] + list(options)):
            self.assertEqual(t.main(), 0)
        return t.load_config(config_path)

    def legacy_config(self, download=False):
        return {'version': 2, 'timezone': 'Asia/Shanghai', 'directions': {
            'upload': {'enabled': not download, 'daily_bytes': 12_500_000_000,
                       'mbps': 3.5, 'region': 'overseas', 'targets': [self.target('/upload', 'post')]},
            'download': {'enabled': download, 'daily_bytes': 20_000_000_000,
                         'mbps': 10, 'region': 'cn', 'targets': [self.target('/file')]},
        }}

    def test_defaults_are_download_only_20gb_5mbps(self):
        t.validate(self.config)
        self.assertEqual(set(self.config['directions']), {'download'})
        self.assertEqual(self.config['directions']['download']['daily_bytes'], 20_000_000_000)
        self.assertEqual(self.config['directions']['download']['mbps'], 5)
        self.assertEqual(set(t.BUILTINS), {'download'})

    def test_upload_rejected_before_any_network_or_reservation(self):
        with self.assertRaisesRegex(ValueError, '仅支持下行'):
            self.transfer('upload')
        self.assertEqual(self.net.calls, [])
        self.assertFalse(self.path.exists())

    def test_legacy_upload_migrates_active_values_not_unused_download_defaults(self):
        old = self.legacy_config()
        config_path = self.directory / 'config.json'
        t.atomic_json(config_path, old)
        updated = self.run_configure('--keep')
        item = updated['directions']['download']
        self.assertEqual(item['daily_bytes'], 12_500_000_000)
        self.assertEqual(item['mbps'], 3.5)
        self.assertEqual(item['region'], 'overseas')
        self.assertEqual(item['targets'], [])
        self.assertEqual(set(updated['directions']), {'download'})
        self.assertEqual(json.loads(config_path.read_text())['version'], 3)

    def test_legacy_active_download_keeps_its_custom_target(self):
        old = self.legacy_config(download=True)
        expected = old['directions']['download']
        self.assertEqual(t.validate(t.migrate_config(old))['directions']['download'], expected)

    def test_cli_saved_nondefaults_control_real_request_quota_and_pacer(self):
        config = self.run_configure('--gb', '0.000000047', '--mbps', '3.25', '--region', 'cn',
                                    '--url', 'http://example.com/file')
        with patch.object(t, 'Pacer', side_effect=NoPacer) as pacer, patch.object(t, 'PIECE', 16):
            t.run_session(config, self.data, self.path, self.directory, self.net)
        pacer.assert_called_once_with(3.25)
        self.assertEqual(self.data['download']['bytes'], 47)
        self.assertEqual(self.data['download']['confirmed'], 47)
        self.assertEqual(len(self.net.calls), 3)
        with patch.object(t, 'transfer', side_effect=AssertionError('quota already completed')):
            t.run_session(config, self.data, self.path, self.directory, self.net)

    def test_interactive_values_persist_and_are_read_back(self):
        with patch('builtins.input', side_effect=['12.5', '3.5', '2', 'https://example.com/data']):
            config = self.run_configure()
        item = config['directions']['download']
        self.assertEqual((item['daily_bytes'], item['mbps'], item['region']), (12_500_000_000, 3.5, 'overseas'))
        self.assertEqual(item['targets'][0]['url'], 'https://example.com/data')

    def test_invalid_cli_values_do_not_overwrite_good_config(self):
        self.run_configure('--gb', '7', '--mbps', '2')
        config_path = self.directory / 'config.json'
        before = config_path.read_bytes()
        for options in [('--gb', 'nan'), ('--mbps', '0'), ('--mbps', 'inf'), ('--gb', '1000')]:
            with self.assertRaises(ValueError):
                self.run_configure(*options)
            self.assertEqual(config_path.read_bytes(), before)

    def test_configuration_changes_preserve_daily_download_ledger(self):
        self.data['download']['bytes'] = 123
        self.data['upload'] = {'bytes': 987, 'confirmed': 980, 'reserved': 0}
        t.atomic_json(self.path, self.data)
        before = self.path.read_bytes()
        self.run_configure('--gb', '8', '--mbps', '2.5')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(t.read_day(self.path, '2026-11-01')['download']['bytes'], 123)

    def test_download_only_test_preserves_daily_quota_and_cleans_test_directory(self):
        self.net.cache = {}
        self.config['directions']['download']['targets'] = [self.target('/file')]
        self.data['download']['bytes'] = 123
        t.atomic_json(self.path, self.data)
        before = self.path.read_bytes()
        with patch.object(t, 'Network', return_value=self.net), patch.object(t, 'Pacer', NoPacer):
            t.real_test(self.config, self.directory, 0.000017, rng=random.Random(0))
        result = json.loads((self.directory / 'last-test.json').read_text())
        self.assertNotIn('upload', result)
        self.assertEqual(result['download']['confirmed'], 17)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.net.calls, [('http://example.com/file', 'cn')])
        self.assertFalse(list(self.directory.glob('test-*')))

    def test_test_failover_requires_full_confirmed_download(self):
        self.net.cache = {}
        self.config['directions']['download']['targets'] = [self.target('/truncated'), self.target('/file')]
        with patch.object(t, 'Network', return_value=self.net), patch.object(t, 'Pacer', NoPacer):
            t.real_test(self.config, self.directory, 0.000017, rng=random.Random(0))
        result = json.loads((self.directory / 'last-test.json').read_text())
        self.assertEqual(result['download']['confirmed'], 17)
        self.assertEqual(result['download']['bytes'], 22)
        self.assertFalse(list(self.directory.glob('test-*')))

    @unittest.skipUnless(os.name == 'posix', 'Linux anonymous file semantics')
    def test_payload_already_unlinked_while_download_is_in_progress(self):
        handles = []
        original = tempfile.TemporaryFile
        def capture(*args, **kwargs):
            handle = original(*args, **kwargs)
            handles.append(handle)
            return handle
        class InspectPacer:
            def wait(inner, size):
                self.assertEqual(os.fstat(handles[-1].fileno()).st_nlink, 0)
                self.assertFalse(list(self.directory.glob('payload-*')))
                raise t.Stop('simulate process interruption')
        with patch.object(t.tempfile, 'TemporaryFile', side_effect=capture):
            with self.assertRaises(t.Stop):
                t.transfer(self.net, 'download', self.item, self.target('/file'), 17,
                           self.directory, self.data, self.path, InspectPacer())
        self.assertTrue(handles[-1].closed)

    def test_source_order_varies_and_does_not_mutate_builtins(self):
        original = list(t.targets(self.config, 'download'))
        orders = [tuple(item['url'] for item in t.shuffled_targets(self.config, 'download', random.Random(seed)))
                  for seed in range(8)]
        self.assertGreater(len(set(orders)), 1)
        self.assertEqual(t.targets(self.config, 'download'), original)
        self.assertEqual(len(original), 6)
        self.assertEqual(len({t.parse_url(item['url']).hostname for item in original}), 6)

    def test_transient_failure_retries_after_delay_and_then_succeeds(self):
        self.config['directions']['download'].update(daily_bytes=17, targets=[self.target('/flaky')])
        health, clock = {}, [1000.0]
        with patch.object(t, 'Pacer', NoPacer), patch.object(t.time, 'time', side_effect=lambda: clock[0]), \
                patch.object(t.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            t.run_session(self.config, self.data, self.path, self.directory, self.net, health=health, rng=random.Random(0))
        self.assertEqual(FixtureHandler.requests['/flaky'], 2)
        self.assertEqual(self.data['download']['confirmed'], 17)
        self.assertGreaterEqual(clock[0], 1002)
        self.assertEqual(health, {})

    def test_repeated_503_is_bounded_to_three_attempts(self):
        self.config['directions']['download'].update(daily_bytes=17, targets=[self.target('/always503')])
        health, clock = {}, [1000.0]
        with patch.object(t, 'Pacer', NoPacer), patch.object(t.time, 'time', side_effect=lambda: clock[0]), \
                patch.object(t.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            with self.assertRaisesRegex(OSError, '最多尝试 3 次'):
                t.run_session(self.config, self.data, self.path, self.directory, self.net, health=health)
        self.assertEqual(FixtureHandler.requests['/always503'], 3)
        self.assertEqual(self.data['download']['bytes'], 0)
        self.assertGreaterEqual(clock[0], 1007)
        self.assertAlmostEqual(next(iter(health.values()))['until'] - clock[0], 300)

    def test_403_and_418_skip_without_hammering_and_keep_cooldown(self):
        for path in ('/always403', '/always418'):
            self.config['directions']['download'].update(daily_bytes=17, targets=[self.target(path)])
            health = {}
            with patch.object(t, 'Pacer', NoPacer), patch.object(t.time, 'time', return_value=1000), \
                    patch.object(t.time, 'sleep', side_effect=AssertionError('denied target must not loop')):
                with self.assertRaises(OSError):
                    t.run_session(self.config, self.data, self.path, self.directory, self.net, health=health)
            self.assertEqual(FixtureHandler.requests[path], 1)
            self.assertEqual(next(iter(health.values()))['until'], 2800)
            self.assertEqual(self.data['download']['bytes'], 0)

    def test_retry_after_defers_request_instead_of_ignoring_server_delay(self):
        self.config['directions']['download'].update(daily_bytes=17, targets=[self.target('/retry-after')])
        health = {}
        with patch.object(t, 'Pacer', NoPacer), patch.object(t.time, 'time', return_value=1000), \
                patch.object(t.time, 'sleep', side_effect=AssertionError('long delay should defer to next session')):
            with self.assertRaisesRegex(OSError, '冷却'):
                t.run_session(self.config, self.data, self.path, self.directory, self.net, health=health)
        self.assertEqual(FixtureHandler.requests['/retry-after'], 1)
        self.assertEqual(next(iter(health.values()))['until'], 1120)

    def test_saved_cooldown_skips_denied_target_in_later_session(self):
        denied, good = self.target('/always403'), self.target('/file')
        self.config['directions']['download'].update(daily_bytes=17, targets=[denied, good])
        health = {denied['url']: {'until': 2000, 'status': 403, 'error': 'denied'}}
        t.atomic_json(self.directory / 'source-health.json', health)
        with patch.object(t.time, 'time', return_value=1000), patch.object(t, 'Pacer', NoPacer):
            loaded = t.load_health(self.directory)
            t.run_session(self.config, self.data, self.path, self.directory, self.net, health=loaded, rng=random.Random(0))
        self.assertNotIn('/always403', FixtureHandler.requests)
        self.assertEqual(self.data['download']['confirmed'], 17)
        with patch.object(t.time, 'time', return_value=3000):
            self.assertEqual(t.load_health(self.directory), {})

    def test_retry_after_http_date_and_invalid_values(self):
        with patch.object(t.time, 'time', return_value=0):
            self.assertEqual(t.retry_delay('Thu, 01 Jan 1970 00:02:00 GMT'), 120)
            self.assertEqual(t.retry_delay('60'), 60)
            for value in ('bad', 'nan', 'inf', '-1', ''):
                self.assertEqual(t.retry_delay(value), 0)


if __name__ == '__main__':
    unittest.main()
