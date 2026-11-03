import contextlib
import datetime as dt
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
from vps_toolkit import agent, backup, forwarder, traffic
from vps_toolkit.cli import main
from vps_toolkit.core import Context, Error, atomic, defaults, read_json, save_config, write_json
from vps_toolkit.health import identify_cloud
from vps_toolkit.network import fetch
from vps_toolkit.platform import detect, service


class Runner:
    def __init__(self):
        self.calls = []
        self.fail_restart = False
    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        code, output = 0, ''
        if argv[:2] == ['systemctl', 'list-unit-files']:
            output = 'realm.service enabled\ntraffic-scheduler.timer enabled\n'
        elif argv[:2] == ['systemctl', 'list-units']:
            output = 'realm.service loaded active running Realm\n'
        elif argv[:2] == ['systemctl', 'restart'] and self.fail_restart:
            self.fail_restart = False
            code = 1
        if kwargs.get('check') and code:
            raise Error('simulated failure')
        return subprocess.CompletedProcess(argv, code, output, '')


class Handler(BaseHTTPRequestHandler):
    payload = bytes(range(256)) * 20000
    ranges = []
    def log_message(self, *args):
        pass
    def do_GET(self):
        start, end = [int(v) for v in self.headers['Range'][6:].split('-')]
        self.ranges.append((start, end))
        if self.path == '/fail':
            self.send_error(403)
            return
        body = self.payload[start:end + 1]
        self.send_response(206)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Content-Range', 'bytes {}-{}/{}'.format(start, end, len(self.payload)))
        self.end_headers()
        self.wfile.write(body)


class LocalNetwork:
    def __init__(self, server):
        self.server, self.cache = server, {}
    def connect(self, url, region):
        from urllib.parse import urlsplit
        from http.client import HTTPConnection
        return HTTPConnection(*self.server.server_address, timeout=2), urlsplit(url), '1.1.1.1', 'CN'


class NoPacer:
    def __init__(self, *args):
        pass
    def wait(self, amount):
        pass


class ToolkitTests(unittest.TestCase):
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
        self.runner = Runner()
        self.ctx = Context(self.temp.name, self.runner)
        self.ctx.require_root = lambda: None
        self.ctx.path('/run/systemd/system').mkdir(parents=True)
        self.ctx.path('/proc').mkdir()
        atomic(self.ctx.path('/etc/os-release'), 'ID=ubuntu\nVERSION_ID="22.04"\n')
        self.cfg = defaults()
        self.cfg['traffic'].update(timezone='system', authorized_urls=['http://example.com/file'], task_bytes=10_000_000)
        save_config(self.ctx, self.cfg)
        self.at = dt.datetime(2026, 11, 3, 12, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        if os.name != 'posix':
            self.lock_patch = patch.object(traffic.engine, 'locked', side_effect=lambda directory: contextlib.nullcontext())
            self.lock_patch.start()
            self.addCleanup(self.lock_patch.stop)
        Handler.ranges = []
    def download(self, **kwargs):
        with patch.object(traffic.engine, 'Pacer', NoPacer):
            return traffic.download(self.ctx, network=LocalNetwork(self.server), at=self.at, **kwargs)
    def test_new_schedule_disabled_and_manual_exact_bytes_with_hash(self):
        self.assertFalse(self.cfg['traffic']['enabled'])
        expected = hashlib.sha256(Handler.payload[:3_000_017]).hexdigest()
        job = self.download(size=3_000_017, expected_sha256=expected)
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['received_bytes'], 3_000_017)
        self.assertEqual(Handler.ranges, [(0, 2_097_151), (2_097_152, 3_000_016)])
        state = traffic.status(self.ctx, self.at)
        self.assertEqual(state['today_bytes'], 3_000_017)
        self.assertFalse(list(self.ctx.path('/var/lib/traffic-scheduler').glob('payload-*')))
        progress_record = read_json(self.ctx.path('/var/lib/traffic-scheduler/progress.json'))
        self.assertEqual(progress_record['status'], 'completed')
        self.assertEqual(progress_record['received_bytes'], 3_000_017)

    def test_ratio_download_stops_after_business_download_eliminates_deficit(self):
        self.cfg['traffic']['strategy'] = 'ratio'
        save_config(self.ctx, self.cfg)
        # First chunk is allowed; another application then fills the deficit.
        with patch.object(traffic.ratio, 'sample', side_effect=[{'needed_bytes': 4_000_000},
                         {'needed_bytes': 0}, {'needed_bytes': 0}]):
            job = self.download(size=4_000_000)
        self.assertEqual(job['status'], 'ratio_satisfied')
        self.assertEqual(job['received_bytes'], 2_097_152)
        self.assertEqual(len(Handler.ranges), 1)
        record = read_json(self.ctx.path('/var/lib/traffic-scheduler/quota-2026-11-03.json'))
        self.assertEqual(record['download']['bytes'], job['received_bytes'])

    def test_ratio_already_satisfied_makes_no_request_and_manual_override_is_explicit(self):
        self.cfg['traffic'].update(strategy='ratio', daily_bytes=100, monthly_bytes=100)
        save_config(self.ctx, self.cfg)
        with patch.object(traffic.ratio, 'sample', return_value={'needed_bytes': 0}):
            self.assertEqual(self.download(size=50)['status'], 'ratio_satisfied')
            self.assertEqual(Handler.ranges, [])
            result = self.download(size=101, ignore_ratio=True)
            self.assertEqual(result['status'], 'budget_exhausted')
            self.assertEqual(result['received_bytes'], 100)

    def test_real_transfer_machine_meter_stops_at_lifetime_deficit_without_double_count(self):
        self.cfg['traffic'].update(strategy='ratio', ratio_tolerance_pct=0)
        save_config(self.ctx, self.cfg)
        quota = self.ctx.path('/var/lib/traffic-scheduler/quota-2026-11-03.json')
        def snapshot(context, selected):
            record = read_json(quota, {'download': {'bytes': 0}})
            return {'boot': 'fixture', 'interfaces': {'eth0': {'tx': 2_000_000,
                    'rx': record['download']['bytes'], 'index': 2}}}
        with patch.object(traffic.ratio, 'auto_interfaces', return_value=['eth0']), patch.object(traffic.ratio, 'snapshot', side_effect=snapshot):
            job = self.download(size=5_000_000)
            state = traffic.status(self.ctx, self.at)
        self.assertEqual(job['status'], 'ratio_satisfied')
        self.assertEqual(job['received_bytes'], 4_000_000)
        self.assertEqual(state['machine_traffic']['download_bytes'], 4_000_000)
        self.assertEqual(state['machine_traffic']['needed_bytes'], 0)
        self.assertEqual(state['today_bytes'], 4_000_000)
        self.assertEqual(Handler.ranges, [(0, 2_097_151), (2_097_152, 3_999_999)])

    def test_ratio_follow_tick_only_when_needed_and_honors_window_budget_and_once_per_minute(self):
        self.cfg['traffic'].update(strategy='ratio', enabled=True, daily_bytes=3_000_000,
                                   monthly_bytes=10_000_000, window=['10:00', '20:00'])
        save_config(self.ctx, self.cfg)
        with patch.object(traffic.ratio, 'sample', return_value={'needed_bytes': 0}), patch.object(traffic, 'download') as transfer:
            self.assertEqual(traffic.tick(self.ctx, self.at)['reason'], 'ratio_satisfied')
            transfer.assert_not_called()
        with patch.object(traffic.ratio, 'sample', return_value={'needed_bytes': 4_000_000}), patch.object(traffic, 'download') as transfer:
            traffic.tick(self.ctx, self.at)
            self.assertEqual(transfer.call_args.kwargs['size'], 3_000_000)
            self.assertTrue(traffic.tick(self.ctx, self.at)['skipped'])
            self.assertTrue(traffic.tick(self.ctx, self.at.replace(hour=23))['skipped'])
            self.assertEqual(transfer.call_count, 1)

    def test_ratio_cli_configure_preserves_existing_state_and_seed_does_not_refund_budget(self):
        with patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(main(['traffic', 'configure', '--strategy', 'ratio', '--ratio', '1:3'], self.ctx), 0)
        saved = read_json(self.ctx.config_path)['traffic']
        self.assertFalse(saved['enabled'])
        self.assertEqual(saved['daily_bytes'], self.cfg['traffic']['daily_bytes'])
        self.assertEqual(saved['ratio'], '1:3')
        directory, path, data, _ = traffic.ledger(self.ctx, saved, self.at)
        data['download']['bytes'] = 12345
        write_json(path, data)
        with patch.object(traffic.ratio, 'seed', return_value={'fixture': True}) as seed, patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(main(['traffic', 'ratio-seed', '--upload-gb', '5', '--download-gb', '6'], self.ctx), 0)
        self.assertEqual(seed.call_args.args[2:], (5_000_000_000, 6_000_000_000))
        self.assertEqual(read_json(path)['download']['bytes'], 12345)
    def test_ratio_cli_normalizes_paste_and_rejects_invalid_setting_without_saving(self):
        with patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(main(['traffic', 'configure', '--strategy', 'ratio', '--ratio', '\ufeff１ ： ２\u200b',
                                   '--mbps', '5', '--ratio-schedule', 'random'], self.ctx), 0)
        saved = read_json(self.ctx.config_path)['traffic']
        self.assertEqual(saved['ratio'], '1:2')
        self.assertEqual(saved['strategy'], 'ratio')
        self.assertEqual(saved['ratio_schedule'], 'random')
        self.assertEqual(saved['daily_bytes'], self.cfg['traffic']['daily_bytes'])
        self.assertFalse(saved['enabled'])
        before = self.ctx.config_path.read_bytes()
        with patch('sys.stderr', new=io.StringIO()):
            self.assertEqual(main(['traffic', 'configure', '--ratio', '1:0'], self.ctx), 1)
        self.assertEqual(self.ctx.config_path.read_bytes(), before)

    def test_daily_and_monthly_budgets_include_legacy_downloads(self):
        self.cfg['traffic'].update(daily_bytes=50, monthly_bytes=60)
        save_config(self.ctx, self.cfg)
        directory = self.ctx.path('/var/lib/traffic-scheduler')
        old = traffic.engine.new_day('2026-11-02')
        old['download']['bytes'] = 35
        write_json(directory / 'quota-2026-11-02.json', old)
        job = self.download(size=40)
        self.assertEqual(job['received_bytes'], 25)
        self.assertEqual(job['status'], 'budget_exhausted')
        with self.assertRaises(Error):
            self.download(size=1)
    def test_task_budget_and_url_authorization_reject_before_network(self):
        with self.assertRaises(Error):
            self.download(size=10_000_001)
        with self.assertRaises(Error):
            self.download(url='https://other.invalid/file', size=1)
        self.assertEqual(Handler.ranges, [])

    def test_legacy_bridge_obeys_global_budget_rate_pause_and_authorization(self):
        self.cfg['traffic'].update(daily_bytes=100, monthly_bytes=100, mbps=2)
        save_config(self.ctx, self.cfg)
        old = traffic.engine.default_config()
        old['directions']['download']['targets'] = [{'name': 'fixture', 'url': 'http://example.com/file', 'kind': 'range'}]
        directory, _, data, _ = traffic.ledger(self.ctx, self.cfg['traffic'], self.at)
        data['download']['bytes'] = 37
        def fixture_configuration(context):
            config = read_json(context.config_path)
            config['traffic']['state_dir'] = str(directory)
            return config
        with patch.object(traffic, 'Context', return_value=self.ctx), patch.object(traffic, 'configuration', side_effect=fixture_configuration):
            self.assertEqual(traffic.legacy_allowance(old, data, directory), 63)
            self.assertEqual(old['directions']['download']['mbps'], 2)
            self.cfg['traffic']['strategy'] = 'ratio'
            save_config(self.ctx, self.cfg)
            with patch.object(traffic.ratio, 'sample', return_value={'needed_bytes': 17}):
                self.assertEqual(traffic.legacy_allowance(old, data, directory), 17)
                self.assertTrue(traffic.legacy_realtime())
            with patch.object(traffic.ratio, 'sample', return_value={'needed_bytes': 0}):
                self.assertEqual(traffic.legacy_allowance(old, data, directory), 0)
            self.cfg['traffic']['paused'] = True
            save_config(self.ctx, self.cfg)
            self.assertEqual(traffic.legacy_allowance(old, data, directory), 0)
            self.cfg['traffic'].update(paused=False, authorized_urls=[])
            save_config(self.ctx, self.cfg)
            with self.assertRaises(Error):
                traffic.legacy_allowance(old, data, directory)

    def test_schedule_window_midnight_gate_and_missed_slots_replanned(self):
        self.cfg['traffic'].update(enabled=True, daily_bytes=1_000_000, task_bytes=100_000,
                                   window=['10:00', '20:00'], mbps=1)
        save_config(self.ctx, self.cfg)
        midnight = self.at.replace(hour=0, minute=0)
        with patch.object(traffic, 'download') as transfer:
            self.assertTrue(traffic.tick(self.ctx, midnight)['skipped'])
            transfer.assert_not_called()
        plan_path = traffic.task_directory(self.ctx) / 'plan-2026-11-03.json'
        plan = read_json(plan_path)
        self.assertEqual(len(plan['minutes']), 10)
        self.assertTrue(all(600 <= n <= 1200 for n in plan['minutes']))
        self.assertGreaterEqual(plan['not_before'], 10)
        later = self.at.replace(hour=18, minute=0)
        with patch.object(traffic, 'download', return_value={'fixture': True}):
            traffic.tick(self.ctx, later)
        repaired = read_json(plan_path)
        self.assertGreaterEqual(sum(n >= 1080 for n in repaired['minutes']), 10)
    def test_checksum_failure_keeps_actual_usage_and_cleans_content(self):
        with self.assertRaises(Error):
            self.download(size=17, expected_sha256='0' * 64)
        path = self.ctx.path('/var/lib/traffic-scheduler/quota-2026-11-03.json')
        self.assertEqual(read_json(path)['download']['bytes'], 17)
        self.assertFalse(list(path.parent.glob('payload-*')))
    def test_download_failure_is_recorded_without_losing_previous_usage(self):
        self.cfg['traffic']['authorized_urls'].append('http://example.com/fail')
        save_config(self.ctx, self.cfg)
        with self.assertRaises(OSError):
            self.download(url='http://example.com/fail', size=10)
        jobs = list((traffic.task_directory(self.ctx) / 'jobs').glob('*.json'))
        self.assertEqual(read_json(jobs[0])['status'], 'failed')
    def test_concurrent_job_lock_is_enforced(self):
        if os.name != 'posix':
            self.skipTest('fcntl requires POSIX')
        directory = self.ctx.path('/var/lib/traffic-scheduler')
        with traffic.engine.locked(directory):
            with self.assertRaises(traffic.engine.Stop):
                self.download(size=10)
    def test_bandwidth_reservation_and_invalid_configuration(self):
        item = self.cfg['traffic']
        item.update(link_mbps=10, reserve_mbps=7, mbps=5)
        traffic.validate(item)
        self.assertEqual(traffic.effective_rate(item), 3)
        for invalid in [float('nan'), 0, -1]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(Error):
                    traffic.validate(dict(item, mbps=invalid))
    def test_pause_blocks_download_and_resume_preserves_usage(self):
        traffic.set_control(self.ctx, 'pause')
        with self.assertRaises(Error):
            self.download(size=17)
        traffic.set_control(self.ctx, 'resume')
        self.assertEqual(self.download(size=17)['received_bytes'], 17)
    def test_fresh_disabled_tick_never_opens_network(self):
        with patch.object(traffic.engine, 'Network', side_effect=AssertionError('must not connect')):
            self.assertTrue(traffic.tick(self.ctx, self.at)['skipped'])
    def test_platform_and_openrc_use_correct_commands(self):
        atomic(self.ctx.path('/etc/os-release'), 'ID=alpine\nVERSION_ID=3.23\n')
        self.ctx.path('/run/systemd/system').rmdir()
        self.ctx.path('/run/openrc').mkdir()
        self.assertEqual(detect(self.ctx)['package_manager'], 'apk')
        service(self.ctx, 'realm', 'start')
        self.assertEqual(self.runner.calls[-1], ['rc-service', 'realm', 'start'])
        self.assertFalse(any('systemctl' in x for x in self.runner.calls))
    def test_cloud_unknown_and_manual_override(self):
        self.assertEqual(identify_cloud(self.ctx)['provider'], 'unknown')
        self.assertEqual(identify_cloud(self.ctx, 'tencent')['confidence'], '用户指定')
    def test_agent_distinguishes_monitor_components_and_protection(self):
        self.cfg['cloud_override'] = 'tencent'
        save_config(self.ctx, self.cfg)
        self.ctx.path('/usr/local/qcloud/monitor/barad').mkdir(parents=True)
        folder = self.ctx.path('/proc/123')
        folder.mkdir()
        atomic(folder / 'comm', 'YDService\n')
        found = {c['id']: c for c in agent.audit(self.ctx)['components']}
        self.assertIn('已安装', found['barad']['state'])
        self.assertEqual(found['sgagent']['state'], '未发现已知组件')
        self.assertEqual(found['yunjing']['state'], '运行中')
        with self.assertRaises(Error):
            agent.remove(self.ctx, 'yunjing', confirmation='yunjing')
    def test_agent_dry_run_and_missing_program_do_not_stop_services(self):
        self.cfg['cloud_override'] = 'aliyun'
        save_config(self.ctx, self.cfg)
        self.ctx.path('/usr/local/aegis').mkdir(parents=True)
        self.assertTrue(agent.remove(self.ctx, 'aegis', dry_run=True)['console_precondition'])
        with self.assertRaises(Error):
            agent.remove(self.ctx, 'aegis', confirmation='aegis', console_authorized=True)
        self.assertFalse(any('stop' in x or 'kill' in x for x in self.runner.calls))
    def test_agent_missing_after_dd_is_not_claim_of_no_monitoring(self):
        result = agent.audit(self.ctx)
        self.assertIn('无法单凭', result['console_notice'])
        self.assertEqual(len(result['layers']), 5)
    def test_cloud_init_dry_run_preserves_netplan_and_requires_second_ack(self):
        network = self.ctx.path('/etc/netplan/50-cloud-init.yaml')
        atomic(network, 'network:\n  version: 2\n')
        self.ctx.path('/etc/cloud').mkdir()
        planned = agent.remove(self.ctx, 'cloud-init', dry_run=True)
        self.assertTrue(planned['requires_second_confirmation'])
        with self.assertRaises(Error):
            agent.remove(self.ctx, 'cloud-init', confirmation='cloud-init')
        self.assertTrue(network.exists())
        self.assertFalse(any(x[:3] == ['apt-get', 'remove', '-y'] for x in self.runner.calls))
    def test_pf_rule_crud_keeps_unknown_configuration_fields(self):
        path = self.ctx.path('/etc/realm/config.json')
        write_json(path, {'log': {'level': 'warn'}, 'custom': 'keep', 'endpoints': []})
        forwarder.rule(self.ctx, 'add', listen='[::]:5555', remote='example.com:443', protocol='udp')
        saved = read_json(path)
        self.assertEqual(saved['custom'], 'keep')
        self.assertTrue(saved['endpoints'][0]['network']['no_tcp'])
        forwarder.rule(self.ctx, 'edit', index=0, remote='1.1.1.1:53', protocol='both')
        forwarder.rule(self.ctx, 'delete', index=0)
        self.assertEqual(read_json(path)['endpoints'], [])
        self.assertFalse(any('restart' in x for x in self.runner.calls))
    def test_pf_failed_restart_rolls_back_configuration(self):
        path = self.ctx.path('/etc/realm/config.json')
        write_json(path, {'endpoints': [], 'custom': 'keep'})
        original = path.read_bytes()
        self.runner.fail_restart = True
        with self.assertRaises(Error):
            forwarder.rule(self.ctx, 'add', listen='127.0.0.1:5555', remote='1.1.1.1:443', apply=True)
        self.assertEqual(path.read_bytes(), original)
    def test_address_rejects_command_injection_and_bad_ports(self):
        for value in ['1.1.1.1:99999', '$(id):80', '127.0.0.1:80\nExecStart=/bin/sh']:
            with self.assertRaises(Error):
                forwarder.address(value)
    def test_backup_restore_does_not_reduce_quota(self):
        quota = self.ctx.path('/var/lib/traffic-scheduler/quota-2026-11-03.json')
        state = traffic.engine.new_day('2026-11-03')
        state['download']['bytes'] = 17
        write_json(quota, state)
        archive = backup.create(self.ctx)['archive']
        state['download']['bytes'] = 51
        write_json(quota, state)
        backup.restore(self.ctx, archive, dry_run=False, confirmation='restore')
        self.assertEqual(read_json(quota)['download']['bytes'], 51)
    def test_backup_rejects_path_traversal(self):
        path = Path(self.temp.name) / 'malicious.tar'
        with tarfile.open(path, 'w') as bundle:
            content = b'{}'
            for name in ['manifest.json', '../../etc/shadow']:
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                bundle.addfile(entry, io.BytesIO(content))
        with self.assertRaises(Error):
            backup.restore(self.ctx, path)

    def test_restore_counts_unsettled_bytes_from_backup(self):
        quota = self.ctx.path('/var/lib/traffic-scheduler/quota-2026-11-03.json')
        state = traffic.engine.new_day('2026-11-03')
        state['download'].update(bytes=20, reserved=17)
        write_json(quota, state)
        archive = backup.create(self.ctx)['archive']
        state['download'].update(bytes=10, reserved=0)
        write_json(quota, state)
        backup.restore(self.ctx, archive, dry_run=False, confirmation='restore')
        self.assertEqual(read_json(quota)['download']['bytes'], 37)
    def test_offline_download_checks_hash_and_rejects_html(self):
        source = Path(self.temp.name) / 'source'
        target = Path(self.temp.name) / 'target'
        source.write_bytes(b'<!doctype html><html>error</html>')
        expected = hashlib.sha256(source.read_bytes()).hexdigest()
        with self.assertRaises(Error):
            fetch(None, target, expected, local=source)
        source.write_bytes(b'#!/bin/sh\nexit 0\n')
        fetch(None, target, hashlib.sha256(source.read_bytes()).hexdigest(), local=source, kind='shell')
        self.assertEqual(target.read_bytes(), source.read_bytes())
        with self.assertRaises(Error):
            fetch(None, target, '0' * 64, local=source)
    def test_elf_architecture_check(self):
        path = Path(self.temp.name) / 'realm'
        data = bytearray(64)
        data[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H', data, 18, 62)
        path.write_bytes(data)
        self.assertEqual(forwarder.inspect_elf(path, 'x86_64')['machine'], 62)
        with self.assertRaises(Error):
            forwarder.inspect_elf(path, 'aarch64')
    def test_noninteractive_configuration_and_status_entry(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['traffic', 'configure', '--daily-gb', '10', '--monthly-gb', '100', '--mbps', '5'], self.ctx), 0)
        updated = read_json(self.ctx.config_path)
        self.assertEqual(updated['traffic']['daily_bytes'], 10_000_000_000)
        self.assertFalse(updated['traffic']['enabled'])
    def test_scheduler_does_not_enable_without_url_and_keeps_old_timer(self):
        self.cfg['traffic']['authorized_urls'] = []
        save_config(self.ctx, self.cfg)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['traffic', 'enable'], self.ctx), 1)
        self.assertFalse(any('stop' in x or 'disable' in x for x in self.runner.calls))

    def test_new_timer_activation_failure_preserves_old_running_task(self):
        def broken_start(argv, **kwargs):
            if argv == ['systemctl', 'start', 'vps-toolkit-traffic.timer']:
                raise Error('simulated new timer failure')
            return self.runner(argv, **kwargs)
        self.ctx.run = broken_start
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['traffic', 'enable', '--replace-legacy'], self.ctx), 1)
        self.assertFalse(read_json(self.ctx.config_path)['traffic']['enabled'])
        self.assertNotIn(['systemctl', 'stop', 'traffic-scheduler.timer'], self.runner.calls)
        self.assertNotIn(['systemctl', 'disable', 'traffic-scheduler.timer'], self.runner.calls)

    def test_corrupt_monthly_record_fails_closed(self):
        atomic(self.ctx.path('/var/lib/traffic-scheduler/quota-2026-11-01.json'), '{broken')
        with self.assertRaises(ValueError):
            self.download(size=17)
        self.assertEqual(Handler.ranges, [])

    def test_unsettled_reservation_is_conservatively_charged(self):
        self.cfg['traffic'].update(daily_bytes=50, monthly_bytes=100)
        save_config(self.ctx, self.cfg)
        state = traffic.engine.new_day('2026-11-03')
        state['download']['reserved'] = 17
        write_json(self.ctx.path('/var/lib/traffic-scheduler/quota-2026-11-03.json'), state)
        job = self.download(size=40)
        self.assertEqual(job['received_bytes'], 33)
        self.assertEqual(read_json(self.ctx.path('/var/lib/traffic-scheduler/quota-2026-11-03.json'))['download']['bytes'], 50)

    def test_agent_cancel_and_failure_report_actual_state(self):
        self.cfg['cloud_override'] = 'tencent'
        save_config(self.ctx, self.cfg)
        script = self.ctx.path('/usr/local/qcloud/monitor/barad/admin/uninstall.sh')
        atomic(script, '#!/bin/sh\nexit 1\n')
        checksum = hashlib.sha256(script.read_bytes()).hexdigest()
        with patch.object(agent, 'trusted_local_script'):
            with self.assertRaises(Error):
                agent.remove(self.ctx, 'barad', confirmation='cancel', sha256=checksum)
            self.assertFalse(any(argv[0] == 'bash' for argv in self.runner.calls))
            def fail_script(argv, **kwargs):
                if argv[0] == 'bash':
                    return subprocess.CompletedProcess(argv, 1, '', 'official failure')
                return self.runner(argv, **kwargs)
            self.ctx.run = fail_script
            result = agent.remove(self.ctx, 'barad', confirmation='barad', sha256=checksum)
            self.assertEqual(result['returncode'], 1)
            self.assertIn('已安装', result['verified']['state'])

    @unittest.skipUnless(os.name == 'posix', 'installer uses POSIX accounts')
    def test_installer_preserves_existing_commands_configs_and_service_states(self):
        import types
        base = Path(__file__).resolve().parents[1]
        if not (base / 'MANIFEST.json').exists():
            base = base.parent.parent / 'outputs/vps-toolkit'
        spec = importlib.util.spec_from_file_location('toolkit_installer', base / 'install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        original = '#!/bin/sh\necho original-pf\n'
        atomic(self.ctx.path('/usr/local/bin/pf'), original, 0o755)
        atomic(self.ctx.path('/etc/systemd/system/realm.service'), 'Original Realm service\n')
        old_unit = self.ctx.path('/etc/systemd/system/traffic-scheduler.service')
        original_unit = '[Service]\nExecStart=/usr/bin/python3 /usr/local/bin/traffic-scheduler tick\nProtectSystem=strict\nReadWritePaths=/var/lib/traffic-scheduler\n'
        atomic(old_unit, original_unit)
        account = types.SimpleNamespace(pw_uid=65534, pw_gid=65534)
        with patch.object(installer.pwd, 'getpwnam', return_value=account), \
                patch.object(installer.grp, 'getgrnam', return_value=types.SimpleNamespace(gr_gid=65534)), \
                patch.object(os, 'chown'):
            installer.install(self.ctx, no_wizard=True)
        self.assertEqual(self.ctx.path('/usr/local/bin/pf').read_text(), original)
        self.assertEqual(self.ctx.path('/etc/systemd/system/realm.service').read_text(), 'Original Realm service\n')
        self.assertEqual(old_unit.read_text(), original_unit)
        dropin = self.ctx.path('/etc/systemd/system/traffic-scheduler.service.d/50-vps-toolkit.conf').read_text()
        self.assertIn('ReadWritePaths=/var/lib/vps-toolkit/traffic', dropin)
        self.assertNotIn('ExecStart=', dropin)
        self.assertNotIn('ProtectSystem=', dropin)
        self.assertFalse(any(operation in argv for argv in self.runner.calls for operation in ('stop', 'start', 'restart', 'enable', 'disable')))
        self.assertFalse(read_json(self.ctx.config_path)['traffic']['enabled'])

    @unittest.skipUnless(os.name == 'posix', 'installer uses POSIX accounts')
    def test_installer_rolls_back_legacy_meter_dropin_if_reload_fails(self):
        import types
        base = Path(__file__).resolve().parents[1]
        if not (base / 'MANIFEST.json').exists():
            base = base.parent.parent / 'outputs/vps-toolkit'
        spec = importlib.util.spec_from_file_location('toolkit_installer_rollback', base / 'install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        atomic(self.ctx.path('/etc/systemd/system/traffic-scheduler.service'),
               '[Service]\nExecStart=/usr/bin/python3 /usr/local/bin/traffic-scheduler tick\n')
        before = self.ctx.config_path.read_bytes()
        def fail_reload(argv, **kwargs):
            if argv == ['systemctl', 'daemon-reload']:
                raise Error('reload failed')
            return self.runner(argv, **kwargs)
        self.ctx.run = fail_reload
        account = types.SimpleNamespace(pw_uid=65534, pw_gid=65534)
        with patch.object(installer.pwd, 'getpwnam', return_value=account), \
                patch.object(installer.grp, 'getgrnam', return_value=types.SimpleNamespace(gr_gid=65534)), \
                patch.object(os, 'chown'), self.assertRaises(Error):
            installer.install(self.ctx, no_wizard=True)
        self.assertFalse(self.ctx.path('/etc/systemd/system/traffic-scheduler.service.d/50-vps-toolkit.conf').exists())
        self.assertEqual(self.ctx.config_path.read_bytes(), before)
        self.assertFalse(any(operation in argv for argv in self.runner.calls for operation in ('stop', 'start', 'restart', 'enable', 'disable')))

    def test_redaction_does_not_export_credentials(self):
        from vps_toolkit.core import redact
        self.assertNotIn('secretvalue', redact('{"password": "secretvalue"}'))
        self.assertNotIn('abc123', redact('Authorization: Bearer abc123'))

    def test_legacy_clock_uses_shared_calendar(self):
        clock = dt.datetime(2026, 11, 2, 23, tzinfo=dt.timezone.utc)
        with patch.object(traffic.engine, 'toolkit_home', return_value=Path('/fixture')), \
                patch.object(traffic, 'moment', return_value=clock) as get_time, \
                patch('vps_toolkit.core.Context', return_value=self.ctx):
            self.assertEqual(traffic.engine.now(), clock)
            get_time.assert_called_once_with('system')

    def test_manual_runtime_is_cut_off_at_calendar_boundary(self):
        late = self.at.replace(hour=23, minute=59, second=58)
        observed = []
        @contextlib.contextmanager
        def deadline(seconds):
            observed.append(seconds)
            yield
        with patch.object(traffic, 'moment', return_value=late), patch.object(traffic.time, 'time', return_value=late.timestamp()), \
                patch.object(traffic.engine, 'deadline', side_effect=deadline), patch.object(traffic.engine, 'Pacer', NoPacer):
            result = traffic.download(self.ctx, size=17, network=LocalNetwork(self.server))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(observed, [2])

    @unittest.skipUnless(os.name == 'posix', 'installer uses POSIX accounts')
    def test_install_disk_full_refuses_before_replacing_commands(self):
        import types
        base = Path(__file__).resolve().parents[1]
        if not (base / 'MANIFEST.json').exists():
            base = base.parent.parent / 'outputs/vps-toolkit'
        spec = importlib.util.spec_from_file_location('disk_full_installer', base / 'install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        command = self.ctx.path('/usr/local/bin/pf')
        atomic(command, '#!/bin/sh\necho old\n')
        with patch.object(installer.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=0)):
            with self.assertRaises(Error):
                installer.install(self.ctx, no_wizard=True)
        self.assertEqual(command.read_text(), '#!/bin/sh\necho old\n')
        self.assertFalse(self.ctx.path('/opt/vps-toolkit/current').exists())

    @unittest.skipUnless(os.name == 'posix', 'installer uses POSIX accounts')
    def test_installer_failed_reload_restores_current_and_own_files(self):
        import types
        base = Path(__file__).resolve().parents[1]
        if not (base / 'MANIFEST.json').exists():
            base = base.parent.parent / 'outputs/vps-toolkit'
        spec = importlib.util.spec_from_file_location('rollback_installer', base / 'install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        original = self.ctx.config_path.read_bytes()
        old_release = self.ctx.path('/opt/vps-toolkit/releases/old')
        old_release.mkdir(parents=True)
        current = old_release.parent.parent / 'current'
        current.symlink_to('releases/old', target_is_directory=True)
        def broken_reload(argv, **kwargs):
            if argv == ['systemctl', 'daemon-reload']:
                raise Error('simulated reload failure')
            return self.runner(argv, **kwargs)
        self.ctx.run = broken_reload
        account = types.SimpleNamespace(pw_uid=65534, pw_gid=65534)
        with patch.object(installer.pwd, 'getpwnam', return_value=account), \
                patch.object(installer.grp, 'getgrnam', return_value=types.SimpleNamespace(gr_gid=65534)), \
                patch.object(os, 'chown'):
            with self.assertRaises(Error):
                installer.install(self.ctx, no_wizard=True)
        self.assertEqual(current.resolve(), old_release.resolve())
        self.assertEqual(self.ctx.config_path.read_bytes(), original)
        self.assertFalse(self.ctx.path('/usr/local/bin/vps-toolkit').exists())
