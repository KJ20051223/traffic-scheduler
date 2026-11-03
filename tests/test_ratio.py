import tempfile
import unittest
from vps_toolkit import ratio
from vps_toolkit.core import Context, Error, atomic, defaults, read_json, write_json


class RatioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = Context(self.temp.name)
        self.item = defaults()['traffic']
        self.item.update(strategy='ratio', ratio_tolerance_pct=0)
        atomic(self.ctx.path('/proc/net/route'),
               'Iface Destination Gateway Flags RefCnt Use Metric Mask\n'
               'eth0 00000000 0100000A 0003 0 0 100 00000000\n'
               'eth1 00000000 0100000A 0003 0 0 200 00000000\n')
        self.counters(5_000_000_000, 6_000_000_000)

    def counters(self, tx, rx, boot='boot-a', index=2):
        atomic(self.ctx.path('/proc/sys/kernel/random/boot_id'), boot)
        atomic(self.ctx.path('/sys/class/net/eth0/ifindex'), str(index))
        atomic(self.ctx.path('/proc/net/dev'),
               'Inter-| Receive | Transmit\n face |bytes packets errs drop fifo frame compressed multicast|bytes\n'
               'eth0: {} 0 0 0 0 0 0 0 {} 0 0 0 0 0 0 0\n'
               'lo: 999999999999 0 0 0 0 0 0 0 999999999999 0 0 0 0 0 0 0\n'
               'docker0: 999999999999 0 0 0 0 0 0 0 999999999999 0 0 0 0 0 0 0\n'.format(rx, tx))

    def sample(self):
        return ratio.sample(self.ctx, self.item)

    def test_current_boot_includes_existing_business_traffic_not_virtual_duplicates(self):
        result = self.sample()
        self.assertEqual(result['needed_bytes'], 4_000_000_000)
        self.assertEqual(result['interfaces'], ['eth0'])
        self.assertFalse(result['calibrated'])
        self.assertTrue(result['warnings'])

    def test_repeated_sampling_no_double_count_and_script_rx_included_once(self):
        self.sample()
        self.counters(5_000_000_000, 7_000_000_000)
        self.assertEqual(self.sample()['needed_bytes'], 3_000_000_000)
        self.assertEqual(self.sample()['needed_bytes'], 3_000_000_000)

    def test_upload_growth_adds_new_deficit_and_excess_download_never_uploads(self):
        self.sample()
        self.counters(6_000_000_000, 6_000_000_000)
        self.assertEqual(self.sample()['needed_bytes'], 6_000_000_000)
        self.counters(6_000_000_000, 13_000_000_000)
        self.assertEqual(self.sample()['needed_bytes'], 0)

    def test_custom_decimal_ratio_and_zero_upload(self):
        self.item['ratio'] = '2.5:3'
        self.assertEqual(self.sample()['target_download_bytes'], 6_000_000_000)
        self.assertEqual(self.sample()['needed_bytes'], 0)
        ratio.seed(self.ctx, self.item, 0, 0)
        self.assertEqual(self.sample()['needed_bytes'], 0)

    def test_tolerance_and_small_deficit_avoid_tiny_repeated_transfers(self):
        self.item['ratio_tolerance_pct'] = 5
        ratio.seed(self.ctx, self.item, 5_000_000_000, 9_600_000_000)
        self.assertEqual(self.sample()['needed_bytes'], 0)
        self.item['ratio_tolerance_pct'] = 0
        self.assertEqual(self.sample()['needed_bytes'], 400_000_000)
        ratio.seed(self.ctx, self.item, 1_000_000, 1_999_999)
        self.assertEqual(self.sample()['needed_bytes'], 0)

    def test_reboot_preserves_totals_and_counts_new_boot_without_subtraction(self):
        self.sample()
        self.counters(100_000_000, 200_000_000, boot='boot-b')
        result = self.sample()
        self.assertEqual(result['upload_bytes'], 5_100_000_000)
        self.assertEqual(result['download_bytes'], 6_200_000_000)
        self.assertEqual(len(result['warnings']), 2)
        self.assertEqual(self.sample()['upload_bytes'], 5_100_000_000)

    def test_single_counter_reset_and_interface_recreation(self):
        self.sample()
        self.counters(5_100_000_000, 100)
        result = self.sample()
        self.assertEqual(result['upload_bytes'], 5_100_000_000)
        self.assertEqual(result['download_bytes'], 6_000_000_100)
        self.counters(200, 300, index=3)
        result = self.sample()
        self.assertEqual(result['upload_bytes'], 5_100_000_200)
        self.assertEqual(result['download_bytes'], 6_000_000_400)

    def test_lifetime_does_not_reset_on_day_month_or_year_boundary(self):
        ratio.sample(self.ctx, self.item, timestamp=1793663999)
        self.counters(5_100_000_000, 6_200_000_000)
        result = ratio.sample(self.ctx, self.item, timestamp=1825199999)
        self.assertEqual(result['upload_bytes'], 5_100_000_000)
        self.assertEqual(result['download_bytes'], 6_200_000_000)
        self.assertEqual(len(list(ratio.state_path(self.ctx).parent.glob('machine-meter*.json'))), 1)

    def test_manual_seed_baseline_not_added_twice_and_previous_record_bounded(self):
        self.sample()
        ratio.seed(self.ctx, self.item, 20_000_000_000, 30_000_000_000)
        self.assertEqual(self.sample()['needed_bytes'], 10_000_000_000)
        self.counters(5_100_000_000, 6_200_000_000)
        result = self.sample()
        self.assertEqual(result['upload_bytes'], 20_100_000_000)
        self.assertEqual(result['download_bytes'], 30_200_000_000)
        self.assertTrue(result['calibrated'])
        ratio.seed(self.ctx, self.item, 20_000_000_000, 30_000_000_000)
        self.assertEqual(len(list(ratio.state_path(self.ctx).parent.glob('machine-meter*.json'))), 2)

    def test_missing_interface_does_not_silently_switch_or_clear(self):
        self.sample()
        before = ratio.state_path(self.ctx).read_bytes()
        atomic(self.ctx.path('/proc/net/dev'), 'lo: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n')
        with self.assertRaises(Error):
            self.sample()
        self.assertEqual(ratio.state_path(self.ctx).read_bytes(), before)

    def test_interface_configuration_change_requires_calibration(self):
        self.sample()
        self.item['ratio_interface'] = 'eth0'
        with self.assertRaises(Error):
            self.sample()
        ratio.seed(self.ctx, self.item, 5_000_000_000, 6_000_000_000)
        self.assertEqual(self.sample()['needed_bytes'], 4_000_000_000)

    def test_corrupt_meter_fails_closed_and_explicit_seed_can_repair(self):
        atomic(ratio.state_path(self.ctx), '{broken')
        with self.assertRaises(Error):
            self.sample()
        ratio.seed(self.ctx, self.item, 5_000_000_000, 6_000_000_000)
        self.assertEqual(self.sample()['needed_bytes'], 4_000_000_000)
        self.assertEqual(ratio.state_path(self.ctx).with_suffix('.previous.json').read_text(), '{broken')
        data = read_json(ratio.state_path(self.ctx))
        data['rx'] = -1
        write_json(ratio.state_path(self.ctx), data)
        with self.assertRaises(Error):
            self.sample()

    def test_ipv6_only_default_route(self):
        self.ctx.path('/proc/net/route').unlink()
        atomic(self.ctx.path('/proc/net/ipv6_route'),
               '{} 00 {} 00 {} 00000400 00000000 00000000 00000003 eth0\n'.format('0'*32, '0'*32, '1'*32))
        self.assertEqual(self.sample()['interfaces'], ['eth0'])

    def test_invalid_ratios_settings_and_interface_path_rejected(self):
        for value in ('0:2', '1:0', '1:-2', 'nan:2', '1:1000', '1/2', '1:2:3'):
            with self.assertRaises(Error):
                ratio.factor(value)
        for name in ('../eth0', 'lo', 'eth0,eth0', 'a/b', ''):
            with self.assertRaises(Error):
                ratio.names(name)
        self.item['ratio_tolerance_pct'] = float('nan')
        with self.assertRaises(Error):
            ratio.validate(self.item)
