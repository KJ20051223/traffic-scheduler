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

    def log_message(self, *args):
        pass

    def do_GET(self):
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

    def do_POST(self):
        length = int(self.headers['Content-Length'])
        received = self.rfile.read(length)
        self.payloads.append(received)
        if self.path == '/blocked':
            body, kind = b'<html>denied</html>', 'text/html'
        elif self.path == '/wrong':
            body, kind = b'{"received_bytes": 1}', 'application/json'
        elif self.path == '/receiver':
            body, kind = json.dumps({'received_bytes': len(received)}).encode(), 'application/json'
        elif self.path == '/speedtest-cn':
            body, kind = ('upload size:' + str(len(received))).encode(), 'text/plain'
        elif self.path == '/bad-speedtest-cn':
            body, kind = b'upload size:1', 'text/plain'
        else:
            body, kind = ('size={}\n'.format(len(received))).encode(), 'text/plain'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
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

    def target(self, path, kind='range'):
        return {'name': 'fixture', 'url': 'http://example.com' + path, 'kind': kind}

    def transfer(self, direction, path='/file', amount=37001, kind='range'):
        return t.transfer(self.net, direction, self.item, self.target(path, kind), amount,
                          self.directory, self.data, self.path, NoPacer())

    def test_default_20gb_upload_10mbps_mainland(self):
        t.validate(self.config)
        self.assertEqual(self.config['directions']['upload']['daily_bytes'], 20_000_000_000)
        self.assertEqual(self.config['directions']['upload']['mbps'], 10)
        self.assertFalse(self.config['directions']['download']['enabled'])

    def test_real_http_download_exact_count_and_cleanup(self):
        self.assertEqual(self.transfer('download'), 37001)
        self.assertEqual(self.data['download']['bytes'], 37001)
        self.assertEqual(self.data['download']['confirmed'], 37001)
        self.assertEqual([p.name for p in self.directory.iterdir()], ['quota.json'])

    def test_real_http_upload_receiving_ack(self):
        self.assertEqual(self.transfer('upload', '/upload', 37001, 'post'), 37001)
        self.assertEqual(len(FixtureHandler.payloads[-1]), 37001)
        self.assertEqual(self.data['upload']['confirmed'], 37001)
        self.assertEqual(self.data['download']['bytes'], 0)

    def test_receiver_requires_matching_ack(self):
        self.transfer('upload', '/receiver', 17, 'receiver')
        self.assertEqual(self.data['upload']['confirmed'], 17)
        with self.assertRaises(OSError):
            self.transfer('upload', '/wrong', 17, 'receiver')
        self.assertEqual(self.data['upload']['bytes'], 34)
        self.assertEqual(self.data['upload']['confirmed'], 17)

    def test_mainland_speedtest_requires_exact_receipt(self):
        self.transfer('upload', '/speedtest-cn', 17, 'speedtest_cn')
        self.assertEqual(self.data['upload']['confirmed'], 17)
        for path in ('/bad-speedtest-cn', '/upload'):
            with self.assertRaises(OSError):
                self.transfer('upload', path, 17, 'speedtest_cn')
        self.assertEqual(self.data['upload']['bytes'], 51)
        self.assertEqual(self.data['upload']['confirmed'], 17)

    def test_block_page_is_not_successful_upload(self):
        with self.assertRaises(OSError):
            self.transfer('upload', '/blocked', 17, 'post')
        self.assertEqual(self.data['upload']['bytes'], 17)
        self.assertEqual(self.data['upload']['confirmed'], 0)

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
        self.data['upload'].update(bytes=3, reserved=7)
        t.atomic_json(self.path, self.data)
        restored = t.read_day(self.path, '2026-11-01')
        t.recover(restored)
        self.assertEqual(restored['upload'], {'bytes': 10, 'confirmed': 0, 'reserved': 0})

    def test_corrupt_state_never_silently_resets(self):
        self.path.write_text('{bad json')
        with self.assertRaises(ValueError):
            t.read_day(self.path, '2026-11-01')

    def test_next_beijing_date_resets_daily_quota(self):
        self.data['upload']['bytes'] = 123
        t.atomic_json(self.path, self.data)
        next_day = t.read_day(self.path, '2026-11-02')
        self.assertEqual(next_day['upload']['bytes'], 0)

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
        self.data['upload']['bytes'] = self.config['directions']['upload']['daily_bytes']
        self.assertFalse(t.due(self.config, self.data, moment))

    def test_slow_measured_network_adds_random_future_slices(self):
        moment = dt.datetime(2026, 11, 1, 8, tzinfo=t.BJ)
        self.data['plan'] = t.make_plan(self.config, self.data, moment, random.Random(1))
        before = set(self.data['plan']['minutes'])
        self.data['upload']['observed_bps'] = 625000
        self.assertEqual(t.remaining_seconds(self.config, self.data), 32000)
        self.assertTrue(t.extend_plan(self.config, self.data, moment, random.Random(2)))
        after = set(self.data['plan']['minutes'])
        self.assertTrue(before <= after)
        self.assertEqual(len(after), 800)
        self.assertTrue(all(n >= 480 for n in after))
        self.assertFalse(t.extend_plan(self.config, self.data, moment))

    def test_corrupt_observed_speed_fails_closed(self):
        self.data['upload']['observed_bps'] = -10
        t.atomic_json(self.path, self.data)
        with self.assertRaises(ValueError):
            t.read_day(self.path, '2026-11-01')

    def test_independent_quotas_and_final_fraction_exact(self):
        self.config['directions']['download']['enabled'] = True
        for direction, size in [('upload', 47), ('download', 31)]:
            self.config['directions'][direction]['daily_bytes'] = size
            self.config['directions'][direction]['targets'] = [self.target('/upload' if direction == 'upload' else '/file',
                                                                              'post' if direction == 'upload' else 'range')]
        with patch.object(t, 'PIECE', 16), patch.object(t, 'Pacer', NoPacer):
            t.run_session(self.config, self.data, self.path, self.directory, self.net)
            self.assertEqual(self.data['upload']['bytes'], 47)
            self.assertEqual(self.data['download']['bytes'], 31)
            with patch.object(t, 'transfer', side_effect=AssertionError('already complete')):
                t.run_session(self.config, self.data, self.path, self.directory, self.net)

    def test_source_failover(self):
        self.config['directions']['upload']['enabled'] = False
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
            pacer = t.Pacer(10)
            pacer.wait(10_000_000)
            self.assertAlmostEqual(clock[0], 8)
            clock[0] = 100
            pacer.wait(1_250_000)
            self.assertAlmostEqual(clock[0], 101)

    def test_reject_impossible_daily_workload(self):
        self.config['directions']['upload']['daily_bytes'] = 1_000_000_000_000
        with self.assertRaises(ValueError):
            t.validate(self.config)

    def test_nonfinite_speed_rejected(self):
        for value in [float('nan'), float('inf'), 0, -1, True]:
            self.config['directions']['upload']['mbps'] = value
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
        self.data['upload']['bytes'] = 123
        self.data['last_tick'] = 720
        self.data['plan'] = {'minutes': [720, 721], 'config_hash': fingerprint, 'jitter': 0}
        ledger = self.directory / 'quota-2026-11-01.json'
        t.atomic_json(ledger, self.data)
        with patch.object(t, 'locked', return_value=contextlib.nullcontext()), \
                patch.object(t, 'now', return_value=moment), \
                patch.object(t, 'run_session', side_effect=AssertionError('duplicate execution')):
            self.assertEqual(t.tick(self.config, self.directory), 0)
        self.assertEqual(t.read_day(ledger, '2026-11-01')['upload']['bytes'], 123)

    def test_upload_only_test_never_downloads(self):
        network = self.net
        network.cache = {}
        self.config['directions']['upload']['targets'] = [self.target('/upload', 'post')]
        with patch.object(t, 'Network', return_value=network), patch.object(t, 'Pacer', NoPacer):
            t.real_test(self.config, self.directory, 0.000017)
        result = json.loads((self.directory / 'last-test.json').read_text())
        self.assertEqual(result['download']['confirmed'], 0)
        self.assertEqual(result['upload']['confirmed'], 17)
        self.assertEqual(network.calls, [('http://example.com/upload', 'cn')])
        self.assertFalse(list(self.directory.glob('test-*')))

    def test_selected_download_runs_before_rejected_upload_in_both_mode(self):
        network = self.net
        network.cache = {}
        self.config['directions']['upload']['targets'] = [self.target('/blocked', 'post')]
        self.config['directions']['download'].update(enabled=True, targets=[self.target('/file')])
        with patch.object(t, 'Network', return_value=network), patch.object(t, 'Pacer', NoPacer):
            with self.assertRaises(OSError):
                t.real_test(self.config, self.directory, 0.000017)
        self.assertEqual(network.calls[0][0], 'http://example.com/file')
        self.assertEqual(network.calls[1][0], 'http://example.com/blocked')
        self.assertFalse((self.directory / 'last-test.json').exists())

    def test_download_only_test_never_uploads(self):
        network = self.net
        network.cache = {}
        self.config['directions']['upload']['enabled'] = False
        self.config['directions']['download'].update(enabled=True, targets=[self.target('/file')])
        with patch.object(t, 'Network', return_value=network), patch.object(t, 'Pacer', NoPacer):
            t.real_test(self.config, self.directory, 0.000017)
        result = json.loads((self.directory / 'last-test.json').read_text())
        self.assertEqual(result['upload']['confirmed'], 0)
        self.assertEqual(result['download']['confirmed'], 17)
        self.assertEqual(network.calls, [('http://example.com/file', 'cn')])

    def test_both_mode_test_requires_each_selected_direction(self):
        network = self.net
        network.cache = {}
        self.config['directions']['upload']['targets'] = [self.target('/upload', 'post')]
        self.config['directions']['download'].update(enabled=True, targets=[self.target('/file')])
        with patch.object(t, 'Network', return_value=network), patch.object(t, 'Pacer', NoPacer):
            t.real_test(self.config, self.directory, 0.000017)
        result = json.loads((self.directory / 'last-test.json').read_text())
        self.assertEqual(result['upload']['confirmed'], 17)
        self.assertEqual(result['download']['confirmed'], 17)

    def test_first_test_failover_still_confirms_full_test_amount(self):
        network = self.net
        network.cache = {}
        self.config['directions']['upload']['targets'] = [self.target('/wrong', 'receiver'),
                                                        self.target('/receiver', 'receiver')]
        with patch.object(t, 'Network', return_value=network), patch.object(t, 'Pacer', NoPacer):
            t.real_test(self.config, self.directory, 0.000017)
        result = json.loads((self.directory / 'last-test.json').read_text())
        self.assertEqual(result['upload']['confirmed'], 17)
        self.assertEqual(result['upload']['bytes'], 34)
        self.assertEqual(result['download']['bytes'], 0)


if __name__ == '__main__':
    unittest.main()
