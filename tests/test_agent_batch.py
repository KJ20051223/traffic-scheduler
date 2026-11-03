import copy
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from vps_toolkit import agent, agent_batch
from vps_toolkit.core import Context, Error, atomic, defaults, digest, read_json, save_config


class AgentBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = Context(self.temp.name, self.run_fixture)
        self.ctx.require_root = lambda: None
        if os.name != 'posix':
            locker = patch.object(agent_batch, 'lock', side_effect=lambda directory: contextlib.nullcontext())
            locker.start()
            self.addCleanup(locker.stop)
        self.present = set()
        self.calls = []
        self.fail_test = self.fail_remove = False
        self.fail_script = None
        atomic(self.ctx.path('/etc/os-release'), 'ID=ubuntu\nVERSION_ID=22.04\n')
        self.ctx.path('/run/systemd/system').mkdir(parents=True)
        self.network = self.ctx.path('/etc/netplan/50-cloud-init.yaml')
        atomic(self.network, 'network:\n  version: 2\n  ethernets:\n    eth0: {dhcp4: true}\n')
        atomic(self.ctx.path('/etc/ssh/sshd_config'), 'Port 22\n')
        save_config(self.ctx, defaults())
        self.audit_patch = patch.object(agent, 'audit', side_effect=lambda ctx: self.facts())
        self.audit_patch.start()
        self.addCleanup(self.audit_patch.stop)
        # Test fixtures belong to the CI account. Check the approved file digest
        # here; root ownership rejection is tested separately below.
        self.trust = patch.object(agent, 'trusted_local_script', side_effect=self.trust_fixture)
        self.trust.start()
        self.addCleanup(self.trust.stop)

    def trust_fixture(self, path, expected):
        if path.is_symlink() or digest(path) != expected:
            raise Error('摘要已改变')

    def facts(self):
        return {'cloud': {'provider': 'tencent'}, 'limitations': [], 'components': [
            dict(id=name, state='运行中' if name in self.present else '未发现已知组件',
                 paths=[item['paths'][0]] if name in self.present else [], processes=[], services=[], packages=[],
                 startup_or_install_records=[]) for name, item in agent.CATALOG.items()]}

    def run_fixture(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv[0] == 'dpkg-query':
            present = argv[-1] in self.present
            return subprocess.CompletedProcess(argv, 0 if present else 1, 'installed' if present else '', '')
        if argv[:3] == ['dpkg', '--no-act', '--remove']:
            return subprocess.CompletedProcess(argv, 1 if self.fail_test else 0, '', 'dependency refuses' if self.fail_test else '')
        if argv[:2] == ['dpkg', '--remove']:
            if not self.fail_remove:
                self.present.discard(argv[-1])
            return subprocess.CompletedProcess(argv, 1 if self.fail_remove else 0, '', 'package failed' if self.fail_remove else '')
        if argv[0] == 'bash':
            path = Path(argv[1])
            component = next((name for name in self.present if any(str(path).startswith(str(self.ctx.path(prefix)))
                              for prefix in agent.CATALOG[name]['paths'])), 'tat')
            failed = self.fail_script == component
            if not failed:
                self.present.discard(component)
            return subprocess.CompletedProcess(argv, 1 if failed else 0, 'official result', '')
        return subprocess.CompletedProcess(argv, 0, '', '')

    def install_script(self, identifier):
        path = self.ctx.path(agent.CATALOG[identifier]['uninstall'][0][0])
        atomic(path, '#!/bin/sh\nexit 0\n')
        self.present.add(identifier)
        return path

    def plan(self, **kwargs):
        return agent_batch.plan(self.ctx, network_independent=True, **kwargs)

    def execute(self, approved):
        return agent_batch.execute(self.ctx, approved, confirmation='remove-all')

    def test_all_scope_includes_cloud_init_and_cloud_agents_monitoring_scope_excludes_them(self):
        self.install_script('barad')
        self.present.update(('tat', 'yunjing', 'cloud-init'))
        planned = self.plan()
        states = {x['id']: x['status'] for x in planned['entries']}
        self.assertEqual(states['barad'], 'ready')
        self.assertEqual(states['cloud-init'], 'ready')
        self.assertEqual(states['yunjing'], 'blocked')
        self.assertEqual(states['tat'], 'ready')
        monitor = self.plan(scope='monitoring')
        self.assertTrue(all(x['status'] == 'excluded' for x in monitor['entries'] if x['id'] in ('tat', 'yunjing', 'cloud-init')))

    def test_cloud_init_removal_keeps_network_ssh_and_backs_up_without_restart(self):
        self.present.add('cloud-init')
        network = self.network.read_bytes()
        result = self.execute(self.plan())
        row = next(r for r in result['results'] if r['id'] == 'cloud-init')
        self.assertEqual(row['status'], 'removed')
        self.assertEqual(result['returncode'], 0)
        self.assertEqual(self.network.read_bytes(), network)
        self.assertTrue(list((self.ctx.state / 'backups').glob('*/50-cloud-init.yaml')))
        self.assertEqual(self.ctx.path('/etc/ssh/sshd_config').read_text(), 'Port 22\n')
        self.assertFalse(any('restart' in argv or '--nodeps' in argv or '--force' in argv for argv, _ in self.calls))

    def test_cloud_init_dependency_refusal_only_disables_next_boot_and_is_not_success(self):
        self.present.add('cloud-init')
        self.fail_test = True
        result = self.execute(self.plan())
        row = next(r for r in result['results'] if r['id'] == 'cloud-init')
        self.assertEqual(row['status'], 'disabled_only')
        self.assertEqual(result['returncode'], 1)
        self.assertTrue(self.ctx.path('/etc/cloud/cloud-init.disabled').is_file())
        self.assertFalse(any(argv[:2] == ['dpkg', '--remove'] for argv, _ in self.calls))

    def test_failed_cloud_init_remove_falls_back_and_persists_actual_partial_result(self):
        self.present.add('cloud-init')
        approved = self.plan()
        self.fail_remove = True
        result = self.execute(approved)
        self.assertEqual(next(r for r in result['results'] if r['id'] == 'cloud-init')['status'], 'disabled_only')
        self.assertEqual(read_json(self.ctx.state / 'agent-removal/latest.json')['returncode'], 1)
        self.assertIn('cloud-init', self.present)

    def test_no_network_ack_or_persistent_config_blocks_cloud_init(self):
        self.present.add('cloud-init')
        planned = agent_batch.plan(self.ctx)
        self.assertEqual(next(x for x in planned['entries'] if x['id'] == 'cloud-init')['status'], 'blocked')
        self.network.unlink()
        self.assertEqual(next(x for x in self.plan()['entries'] if x['id'] == 'cloud-init')['status'], 'blocked')

    def test_cancel_causes_no_uninstall_or_report_mutation(self):
        self.install_script('barad')
        approved = self.plan()
        self.calls.clear()
        with self.assertRaises(Error):
            agent_batch.execute(self.ctx, approved, confirmation='cancel')
        self.assertEqual(self.calls, [])
        self.assertFalse((self.ctx.state / 'agent-removal').exists())

    def test_one_failure_does_not_prevent_other_components_and_each_is_verified(self):
        self.install_script('sgagent')
        self.install_script('barad')
        self.fail_script = 'sgagent'
        result = self.execute(self.plan())
        states = {r['id']: r['status'] for r in result['results']}
        self.assertEqual(states['sgagent'], 'failed')
        self.assertEqual(states['barad'], 'removed')
        self.assertEqual(result['returncode'], 1)
        self.assertIn('sgagent', self.present)
        calls = [(argv, kwargs) for argv, kwargs in self.calls if argv[0] == 'bash']
        self.assertTrue(all(kwargs['cwd'] == str(Path(argv[1]).parent) for argv, kwargs in calls))

    def test_script_changed_after_approval_is_not_executed(self):
        path = self.install_script('barad')
        approved = self.plan()
        atomic(path, '#!/bin/sh\nexit 9\n')
        result = self.execute(approved)
        self.assertEqual(next(r for r in result['results'] if r['id'] == 'barad')['status'], 'failed')
        self.assertFalse(any(argv[0] == 'bash' for argv, _ in self.calls))

    def test_protected_agent_requires_console_flow_and_runs_its_official_program(self):
        self.present.add('yunjing')
        path = self.ctx.path('/usr/local/qcloud/YunJing/uninst.sh')
        atomic(path, '#!/bin/sh\nexit 0\n')
        blocked = self.execute(self.plan())
        self.assertEqual(next(r for r in blocked['results'] if r['id'] == 'yunjing')['status'], 'blocked')
        self.assertFalse(any(argv[0] == 'bash' for argv, _ in self.calls))
        result = self.execute(self.plan(console_authorized=True))
        self.assertTrue(any(argv[:2] == ['bash', str(path)] for argv, _ in self.calls))

    def test_bundled_tat_is_pinned_and_works_without_network_fetch(self):
        self.present.add('tat')
        approved = self.plan()
        entry = next(x for x in approved['entries'] if x['id'] == 'tat')
        self.assertEqual(entry['steps'][0]['sha256'], agent_batch.TAT_SHA256)
        result = self.execute(approved)
        self.assertEqual(next(r for r in result['results'] if r['id'] == 'tat')['status'], 'removed')
        self.assertFalse(any(argv[0] in ('curl', 'wget') for argv, _ in self.calls))

    def test_unknown_vendor_history_only_and_newly_discovered_agents_are_not_removed(self):
        self.install_script('barad')
        approved = self.plan()
        self.present.add('sgagent')
        self.execute(approved)
        self.assertIn('sgagent', self.present)
        facts = self.facts()
        facts['cloud']['provider'] = 'unknown'
        with patch.object(agent, 'audit', return_value=facts):
            state = next(x for x in self.plan()['entries'] if x['id'] == 'sgagent')['status']
        self.assertEqual(state, 'blocked')

    def test_zero_exit_with_residual_agent_is_reported_as_residual(self):
        self.install_script('barad')
        approved = self.plan()
        original = self.ctx.run
        self.ctx.run = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, '', '') if argv[0] == 'bash' else original(argv, **kwargs)
        result = self.execute(approved)
        self.assertEqual(next(r for r in result['results'] if r['id'] == 'barad')['status'], 'residual')
        self.assertEqual(result['returncode'], 1)

    def test_cloud_assistant_official_cleanup_is_confined_to_its_directories(self):
        self.present.add('aliyun-assist')
        own = self.ctx.path('/usr/local/share/aliyun-assist/version/log.txt')
        unrelated = self.ctx.path('/usr/local/share/other-app/keep.txt')
        atomic(own, 'agent log\n')
        atomic(unrelated, 'keep\n')
        original = self.facts
        def facts():
            value = original()
            value['cloud']['provider'] = 'aliyun'
            return value
        with patch.object(agent, 'audit', side_effect=lambda ctx: facts()):
            result = self.execute(self.plan())
        self.assertEqual(next(r for r in result['results'] if r['id'] == 'aliyun-assist')['status'], 'removed')
        self.assertFalse(own.exists())
        self.assertEqual(unrelated.read_text(), 'keep\n')
        self.assertTrue(self.network.exists())

    def test_network_config_change_after_confirmation_prevents_cloud_init_mutation(self):
        self.present.add('cloud-init')
        approved = self.plan()
        atomic(self.network, 'network: changed\n')
        self.calls.clear()
        result = self.execute(approved)
        self.assertEqual(next(r for r in result['results'] if r['id'] == 'cloud-init')['status'], 'failed')
        self.assertFalse(any(argv[:2] == ['dpkg', '--remove'] for argv, _ in self.calls))
        self.assertFalse(self.ctx.path('/etc/cloud/cloud-init.disabled').exists())

    def test_local_program_permission_checks_are_preserved(self):
        self.trust.stop()
        path = SimpleNamespace(is_file=lambda: True, is_symlink=lambda: False,
                               stat=lambda: SimpleNamespace(st_uid=1000, st_mode=0o600))
        with self.assertRaises(Error):
            agent.trusted_local_script(path, 'hash')
        path.stat = lambda: SimpleNamespace(st_uid=0, st_mode=0o666)
        with self.assertRaises(Error):
            agent.trusted_local_script(path, 'hash')

