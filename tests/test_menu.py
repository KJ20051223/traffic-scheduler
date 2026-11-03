import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from vps_toolkit import cli, menu, presentation
from vps_toolkit.core import Context, defaults, read_json, save_config, write_json


class Terminal(io.StringIO):
    def isatty(self):
        return True


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = Context(self.temp.name, lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, '', ''))
        self.ctx.require_root = lambda: None
        save_config(self.ctx, defaults())
        self.out, self.err = Terminal(), io.StringIO()
        self.snapshots = []

    def interact(self, answers):
        answers = iter(answers)
        def respond(prompt):
            self.snapshots.append((prompt, self.out.getvalue()))
            return next(answers)
        with patch('sys.stdout', self.out), patch('sys.stderr', self.err), patch('builtins.input', side_effect=respond):
            return cli.main([], self.ctx)

    def test_result_clears_screen_and_stays_until_zero_with_no_json(self):
        self.assertEqual(self.interact(['2', 'x', '0', '0']), 0)
        self.assertEqual(self.out.getvalue().count('/  主菜单'), 2)
        self.assertEqual(self.out.getvalue().count('/  查看云平台信息'), 1)
        waits = [text for prompt, text in self.snapshots if '输入 0 返回' in prompt]
        self.assertEqual(len(waits), 2)
        self.assertTrue(all(text.count('/  主菜单') == 1 for text in waits))
        self.assertIn('云平台：未能识别', waits[0])
        self.assertTrue(waits[0].rfind('\033[2J\033[H') > waits[0].find('0. 退出工具'))
        self.assertNotIn('"provider"', self.out.getvalue())
        self.assertNotIn('"exit"', self.out.getvalue())

    def test_nested_numeric_menu_zero_returns_to_parent_then_main(self):
        self.assertEqual(self.interact(['10', '4', '0', '0', '0']), 0)
        self.assertEqual(self.out.getvalue().count('/  下载任务调度'), 2)
        self.assertEqual(self.out.getvalue().count('/  解除暂停'), 1)
        self.assertIn('操作：解除暂停', self.out.getvalue())
        self.assertFalse(read_json(self.ctx.config_path)['traffic']['paused'])

    def test_numeric_ratio_configuration_preserves_budget_and_task_state(self):
        before = read_json(self.ctx.config_path)['traffic']
        self.assertEqual(self.interact(['10', '8', '1:3', '', '2', '1', '0', '0', '0']), 0)
        after = read_json(self.ctx.config_path)['traffic']
        self.assertEqual(after['ratio'], '1:3')
        self.assertEqual(after['mbps'], 2)
        self.assertEqual(after['daily_bytes'], before['daily_bytes'])
        self.assertEqual(after['enabled'], before['enabled'])
        self.assertIn('目标上行:下行：1:3', self.out.getvalue())

    def test_busy_or_failed_action_waits_on_result_page_instead_of_crashing(self):
        from vps_toolkit import progress
        with patch.object(progress, 'snapshot', side_effect=cli.Error('已有任务正在运行')):
            self.assertEqual(self.interact(['11', '0', '0']), 0)
        self.assertIn('已有任务正在运行', self.err.getvalue() + self.out.getvalue())
        waits = [text for prompt, text in self.snapshots if '输入 0 返回' in prompt]
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0].count('/  主菜单'), 1)

    def test_ratio_form_accepts_reported_input_paste_and_default(self):
        before = read_json(self.ctx.config_path)['traffic']
        for supplied in ('1:2 ', '１：２', '\ufeff1：2\u200b', ''):
            with self.subTest(supplied=repr(supplied)):
                self.assertEqual(self.interact(['10', '8', supplied, '', '', '2', '0', '0', '0']), 0)
                after = read_json(self.ctx.config_path)['traffic']
                self.assertEqual(after['strategy'], 'ratio')
                self.assertEqual(after['ratio'], '1:2')
                self.assertEqual(after['ratio_schedule'], 'random')
                for key in ('daily_bytes', 'monthly_bytes', 'task_bytes', 'mbps', 'enabled', 'paused'):
                    self.assertEqual(after[key], before[key])
        self.assertNotIn('失败:', self.err.getvalue())

    def test_invalid_ratio_reprompts_before_collecting_other_settings(self):
        self.assertEqual(self.interact(['10', '8', '1/2', '1:2', '', '', '2', '0', '0', '0']), 0)
        self.assertEqual(read_json(self.ctx.config_path)['traffic']['ratio'], '1:2')
        prompts = [prompt for prompt, _ in self.snapshots]
        ratio_positions = [i for i, prompt in enumerate(prompts) if prompt.startswith('目标比例')]
        interface_position = next(i for i, prompt in enumerate(prompts) if prompt.startswith('计量出口网卡'))
        self.assertEqual(len(ratio_positions), 2)
        self.assertLess(ratio_positions[-1], interface_position)
        self.assertIn('输入有误：比例格式', self.out.getvalue())

    def test_invalid_ratio_can_return_without_saving_or_asking_other_fields(self):
        before = self.ctx.config_path.read_bytes()
        self.assertEqual(self.interact(['10', '8', '1/2', '0', '0', '0']), 0)
        self.assertEqual(self.ctx.config_path.read_bytes(), before)
        self.assertFalse(any(prompt.startswith('计量出口网卡') for prompt, _ in self.snapshots))
        self.assertFalse(any('输入 0 返回上一级' in prompt for prompt, _ in self.snapshots))

    def test_budget_edit_in_ratio_mode_keeps_strategy_ratio_meter_and_task_state(self):
        cfg = read_json(self.ctx.config_path)
        cfg['traffic'].update(strategy='ratio', ratio='1:3', enabled=True, paused=True, ratio_schedule='random')
        save_config(self.ctx, cfg)
        meter = self.ctx.state / 'traffic/machine-meter.json'
        write_json(meter, {'preserved': True})
        self.assertEqual(self.interact(['10', '7', '10', '100', '5', '0', '0', '0']), 0)
        item = read_json(self.ctx.config_path)['traffic']
        for key in ('strategy', 'ratio', 'enabled', 'paused', 'ratio_schedule'):
            self.assertEqual(item[key], cfg['traffic'][key])
        self.assertEqual((item['daily_bytes'], item['monthly_bytes'], item['mbps']), (10e9, 100e9, 5))
        self.assertEqual(read_json(meter), {'preserved': True})
        self.assertIn('保留当前模式：累计比例补下行', self.out.getvalue())

    def test_explicit_mode_switch_is_separate_from_budget_settings(self):
        cfg = read_json(self.ctx.config_path)
        cfg['traffic'].update(strategy='ratio', ratio='1:3', enabled=True)
        save_config(self.ctx, cfg)
        self.assertEqual(self.interact(['10', '15', '1', '0', '0', '0']), 0)
        item = read_json(self.ctx.config_path)['traffic']
        self.assertEqual(item['strategy'], 'quota')
        for key in ('ratio', 'daily_bytes', 'monthly_bytes', 'mbps', 'enabled'):
            self.assertEqual(item[key], cfg['traffic'][key])

    def test_manual_interface_is_requested_only_when_auto_read_fails(self):
        from vps_toolkit import traffic
        ready = {'status': 'ready', 'note': '已自动读取'}
        failed = {'status': 'unavailable', 'note': '无法识别出口网卡', 'manual_interface': True}
        with patch.object(traffic, 'initialize_meter', side_effect=[failed, failed, ready]):
            self.assertEqual(self.interact(['10', '8', '1:2', '', '', '2', 'eth0', '0', '0', '0']), 0)
        self.assertEqual(read_json(self.ctx.config_path)['traffic']['ratio_interface'], 'eth0')
        self.assertTrue(any(prompt.startswith('请手动指定出口网卡') for prompt, _ in self.snapshots))
        self.snapshots.clear()
        with patch.object(traffic, 'initialize_meter', return_value=ready):
            self.assertEqual(self.interact(['10', '8', '1:2', '', '', '2', '0', '0', '0']), 0)
        self.assertFalse(any(prompt.startswith('请手动指定出口网卡') for prompt, _ in self.snapshots))

    def test_invalid_form_value_does_not_exit_menu_or_change_config(self):
        before = self.ctx.config_path.read_bytes()
        self.assertEqual(self.interact(['10', '7', 'abc', '', '', '0', '0', '0']), 0)
        self.assertIn('输入有误', self.err.getvalue())
        self.assertNotIn('usage:', self.err.getvalue())
        self.assertEqual(self.ctx.config_path.read_bytes(), before)

    def test_eof_and_ctrl_c_exit_cleanly_and_legacy_exit_number_still_works(self):
        for error in (EOFError(), KeyboardInterrupt()):
            with patch('sys.stdout', self.out), patch('builtins.input', side_effect=error):
                self.assertEqual(cli.main([], self.ctx), 0)
        self.assertEqual(self.interact(['19']), 0)
        self.assertNotIn('Traceback', self.out.getvalue())

    def test_noninteractive_json_is_preserved_and_human_flag_is_readable(self):
        output = io.StringIO()
        with patch('sys.stdout', output):
            self.assertEqual(cli.main(['cloud', 'show'], self.ctx), 0)
        self.assertEqual(json.loads(output.getvalue())['provider'], 'unknown')
        output = io.StringIO()
        with patch('sys.stdout', output):
            self.assertEqual(cli.main(['--human', 'cloud', 'show'], self.ctx), 0)
        self.assertIn('云平台：', output.getvalue())
        self.assertNotIn('"provider"', output.getvalue())

    def test_redirected_output_does_not_emit_clear_escape_sequences(self):
        with patch('sys.stdout', io.StringIO()) as out:
            menu.screen('测试结果')
        self.assertNotIn('\033', out.getvalue())

    def test_scheduler_progress_option_returns_with_one_zero(self):
        from vps_toolkit import progress
        with patch.object(progress, 'watch', return_value=False) as watch:
            self.assertEqual(self.interact(['10', '14', '0', '0']), 0)
        watch.assert_called_once_with(self.ctx)
        self.assertIn('实时下载进度', self.out.getvalue())

    def test_plain_language_reports_keep_agent_limitations_ratio_and_rules(self):
        out = io.StringIO()
        with patch('sys.stdout', out):
            presentation.render({'components': [{'id': 'barad', 'state': '运行中', 'paths': ['/var/barad']}],
                                 'limitations': ['部分信息不可读取'], 'console_notice': '请核对控制台心跳'})
            presentation.render({'endpoints': [{'listen': '0.0.0.0:1234', 'remote': 'host:443',
                                               'network': {'use_udp': True}}]})
            presentation.render({'machine_traffic': {'target_ratio': '1:2', 'needed_bytes': 4_000_000_000}})
        for text in ('腾讯云监控 Barad：运行中', '部分信息不可读取', '请核对控制台心跳',
                     '0.0.0.0:1234 → host:443', '目标上行:下行：1:2', '待补下行：4.000 GB'):
            self.assertIn(text, out.getvalue())

    @unittest.skipUnless(os.name == 'posix', 'real POSIX download child path')
    def test_low_privilege_download_child_inherits_human_mode_and_explicit_override(self):
        import pwd
        from types import SimpleNamespace
        args = cli.parser().parse_args(['--human', 'traffic', 'test', '--mb', '10', '--ignore-ratio'])
        with patch.object(os, 'geteuid', return_value=0), patch.object(pwd, 'getpwnam', return_value=SimpleNamespace(pw_name='traffic-scheduler')), patch.object(cli.subprocess, 'call', return_value=0) as child:
            self.assertEqual(cli.download_child(Context(), args), {'returncode': 0})
        argv = child.call_args.args[0]
        self.assertLess(argv.index('--human'), argv.index('traffic'))
        self.assertIn('--ignore-ratio', argv)

    def test_control_sequences_from_command_output_cannot_clear_result_screen(self):
        out = io.StringIO()
        with patch('sys.stdout', out):
            presentation.render({'output': '\033[2Jhello\033[0m\x00'})
        self.assertIn('hello', out.getvalue())
        self.assertNotIn('\033', out.getvalue())
        self.assertNotIn('\x00', out.getvalue())
