import datetime as dt
import io
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from vps_toolkit import menu, presentation, progress, ratio, traffic
from vps_toolkit.core import Context, defaults, read_json, save_config, write_json


class Terminal(io.StringIO):
    def isatty(self):
        return True


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = Context(self.temp.name)
        self.cfg = defaults()
        save_config(self.ctx, self.cfg)
        self.at = dt.datetime(2026, 11, 3, 12, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        self.directory = self.ctx.path(self.cfg['traffic']['state_dir'])
        self.data = traffic.engine.new_day(self.at.date().isoformat())

    def test_no_state_snapshot_does_not_create_files_or_acquire_quota_lock(self):
        before = sorted(str(p) for p in self.ctx.root.rglob('*'))
        with patch.object(traffic.engine, 'locked', side_effect=AssertionError('must not lock')):
            result = progress.snapshot(self.ctx, self.at)
        self.assertEqual(result['today_bytes'], 0)
        self.assertFalse(result['active'])
        self.assertEqual(before, sorted(str(p) for p in self.ctx.root.rglob('*')))

    def test_persisted_between_task_cooldown_is_visible_without_a_live_process(self):
        write_json(self.directory / 'cooldown.json', {'until': time.time() + 7})
        result = progress.snapshot(self.ctx, self.at)
        self.assertEqual(result['status'], 'cooldown')
        self.assertGreater(result['cooldown_remaining_seconds'], 5)
        self.assertEqual(result['speed_mbps'], 0)
        self.assertFalse(result['active'])

    def test_active_progress_sees_inflight_bytes_percent_and_speed_without_settling_quota(self):
        self.data['download'].update(bytes=2_000_000, reserved=2_000_000)
        path = self.directory / 'quota-2026-11-03.json'
        write_json(path, self.data)
        identifier = 'a' * 32
        write_json(traffic.task_directory(self.ctx) / 'active.json', {'id': identifier})
        write_json(traffic.task_directory(self.ctx) / 'jobs' / (identifier + '.json'),
                   {'id': identifier, 'requested_bytes': 10_000_000, 'received_bytes': 2_000_000, 'status': 'running'})
        writer = progress.reporter(self.directory, self.data, 10_000_000, identifier, 2_000_000)
        writer(1_000_000, 2)
        with patch.object(progress, 'alive', return_value=True), patch.object(traffic.engine, 'locked', side_effect=AssertionError('busy lock')):
            result = progress.snapshot(self.ctx, self.at)
        self.assertTrue(result['active'])
        self.assertEqual(result['percent'], 30)
        self.assertEqual(result['today_bytes'], 3_000_000)
        self.assertEqual(result['speed_mbps'], 4)
        self.assertEqual(result['eta_seconds'], 14)
        self.assertEqual(result['task_remaining_bytes'], 7_000_000)
        self.assertEqual(read_json(path)['download']['bytes'], 2_000_000)
        self.assertEqual(read_json(path)['download']['reserved'], 2_000_000)

    def test_legacy_chunk_progress_is_visible_and_finished_process_is_not_active(self):
        writer = progress.reporter(self.directory, self.data, requested=2_000_000)
        writer(1_000_000, 2)
        with patch.object(progress, 'alive', return_value=True):
            result = progress.snapshot(self.ctx, self.at)
        self.assertIn('兼容旧入口', result['entry'])
        self.assertEqual(result['percent'], 50)
        with patch.object(progress, 'alive', return_value=False):
            stale = progress.snapshot(self.ctx, self.at)
        self.assertFalse(stale['active'])
        self.assertEqual(stale['speed_mbps'], 0)

    def test_finished_snapshot_reports_zero_speed_and_persistent_final_job(self):
        identifier = 'b' * 32
        writer = progress.reporter(self.directory, self.data, 2_000_000, identifier)
        writer(2_000_000, 2, final=True)
        write_json(traffic.task_directory(self.ctx) / 'jobs' / (identifier + '.json'),
                   {'status': 'completed', 'requested_bytes': 2_000_000, 'received_bytes': 2_000_000})
        progress.finish(self.directory, 'completed')
        result = progress.snapshot(self.ctx, self.at)
        self.assertFalse(result['active'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['percent'], 100)
        self.assertEqual(result['speed_mbps'], 0)

    def test_ratio_preview_includes_new_business_bytes_without_modifying_meter(self):
        self.cfg['traffic']['strategy'] = 'ratio'
        save_config(self.ctx, self.cfg)
        raw = {'boot': 'fixture', 'interfaces': {'eth0': {'tx': 5_000_000_000, 'rx': 6_000_000_000, 'index': 2}}}
        with patch.object(ratio, 'auto_interfaces', return_value=['eth0']), patch.object(ratio, 'snapshot', return_value=raw):
            ratio.seed(self.ctx, self.cfg['traffic'], 5_000_000_000, 6_000_000_000)
        before = ratio.state_path(self.ctx).read_bytes()
        new = {'boot': 'fixture', 'interfaces': {'eth0': {'tx': 6_000_000_000, 'rx': 7_000_000_000, 'index': 2}}}
        with patch.object(ratio, 'snapshot', return_value=new):
            result = progress.snapshot(self.ctx, self.at)
        self.assertEqual(result['machine_traffic']['needed_bytes'], 5_000_000_000)
        self.assertEqual(ratio.state_path(self.ctx).read_bytes(), before)

    def test_watch_zero_returns_without_a_second_back_prompt(self):
        view = progress.snapshot(self.ctx, self.at)
        stdin, stdout = Terminal('0\n'), Terminal()
        with patch('sys.stdin', stdin), patch('sys.stdout', stdout), patch.object(progress, 'snapshot', return_value=view), patch.object(progress.select, 'select', return_value=([stdin], [], [])), patch.object(menu, 'wait_back', side_effect=AssertionError('must not ask twice')):
            menu.action_page(self.ctx, '实时下载进度', lambda: progress.watch(self.ctx))
        self.assertIn('每秒刷新', stdout.getvalue())
        self.assertEqual(stdout.getvalue().count('今日已结算'), 1)

    def test_first_ratio_preview_reads_machine_totals_and_remaining_without_writing_baseline(self):
        self.cfg['traffic'].update(strategy='ratio', daily_bytes=2_000_000_000)
        save_config(self.ctx, self.cfg)
        raw = {'boot': 'fixture', 'interfaces': {'eth0': {'tx': 5_000_000_000, 'rx': 6_000_000_000, 'index': 2}}}
        before = sorted(str(p) for p in self.ctx.root.rglob('*'))
        with patch.object(ratio, 'auto_interfaces', return_value=['eth0']), patch.object(ratio, 'snapshot', return_value=raw), patch.object(traffic.engine, 'locked', side_effect=AssertionError('must not lock')):
            result = progress.snapshot(self.ctx, self.at)
        machine = result['machine_traffic']
        self.assertEqual(machine['upload_bytes'], 5_000_000_000)
        self.assertEqual(machine['download_bytes'], 6_000_000_000)
        self.assertEqual(machine['needed_bytes'], 4_000_000_000)
        self.assertEqual(machine['fillable_now_bytes'], 2_000_000_000)
        self.assertTrue(machine['limited_by_budget'])
        self.assertFalse(machine['baseline_persisted'])
        self.assertEqual(before, sorted(str(p) for p in self.ctx.root.rglob('*')))
        output = io.StringIO()
        with patch('sys.stdout', output):
            presentation.render_progress(result)
        self.assertIn('达到比例还需补下载（含容差）：4.000 GB', output.getvalue())
        self.assertIn('当前预算内最多可补：2.000 GB', output.getvalue())

    def test_ratio_satisfied_never_uses_remaining_budget_as_a_download_target(self):
        self.cfg['traffic']['strategy'] = 'ratio'
        save_config(self.ctx, self.cfg)
        raw = {'boot': 'fixture', 'interfaces': {'eth0': {'tx': 5_000_000_000, 'rx': 11_000_000_000, 'index': 2}}}
        with patch.object(ratio, 'auto_interfaces', return_value=['eth0']), patch.object(ratio, 'snapshot', return_value=raw):
            result = progress.snapshot(self.ctx, self.at)
        self.assertGreater(result['remaining_bytes'], 0)
        self.assertEqual(result['machine_traffic']['needed_bytes'], 0)
        self.assertEqual(result['machine_traffic']['fillable_now_bytes'], 0)

    def test_machine_read_failure_reports_error_and_never_invents_totals(self):
        self.cfg['traffic']['strategy'] = 'ratio'
        save_config(self.ctx, self.cfg)
        with patch.object(ratio, 'auto_interfaces', side_effect=traffic.Error('无法识别出口网卡')):
            result = progress.snapshot(self.ctx, self.at)
        self.assertNotIn('upload_bytes', result['machine_traffic'])
        self.assertIn('无法识别出口网卡', result['machine_traffic']['note'])

    def test_progress_display_failure_does_not_abort_transfer_writer(self):
        writer = progress.reporter(self.directory, self.data)
        with patch.object(progress, 'write_json', side_effect=OSError('read only display')):
            writer(1_000_000, 1, final=True)

