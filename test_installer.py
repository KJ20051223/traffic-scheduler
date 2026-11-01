import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == 'posix', 'installer uses Linux/Bash')
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.source = Path(__file__).resolve().parent
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def shell(self, code, *args):
        return subprocess.run(['bash', '-c', 'set -Eeuo pipefail\nsource "$1"\n' + code,
                               'test', str(self.source / 'installer_platform.sh')] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def release(self, identifier, version, like=''):
        path = self.root / 'os-release'
        path.write_text('ID={}\nVERSION_ID="{}"\nID_LIKE="{}"\nPRETTY_NAME="fixture"\n'.format(
            identifier, version, like), encoding='utf-8')
        return str(path)

    def test_supported_distributions_and_derivatives_choose_package_family(self):
        fixtures = [('ubuntu', '20.04', '', 'apt'), ('ubuntu', '24.04', '', 'apt'),
                    ('debian', '11', '', 'apt'), ('debian', '13', '', 'apt'),
                    ('rhel', '8.10', '', 'rpm'), ('rocky', '9.6', '', 'rpm'),
                    ('almalinux', '10', '', 'rpm'), ('ol', '9.6', '', 'rpm'),
                    ('centos', '9', 'rhel fedora', 'rpm'), ('fedora', '44', '', 'rpm'),
                    ('amzn', '2023', '', 'rpm'), ('opensuse-leap', '15.6', '', 'zypper'),
                    ('opensuse-leap', '16.0', '', 'zypper'),
                    ('opensuse-tumbleweed', '20261030', '', 'zypper'),
                    ('arch', '', '', 'pacman'), ('manjaro', '26', 'arch', 'pacman'),
                    ('linuxmint', '22', 'ubuntu debian', 'apt'),
                    ('custom', '1', 'rhel fedora', 'rpm')]
        for identifier, version, like, family in fixtures:
            with self.subTest(identifier=identifier, version=version):
                result = self.shell('platform_detect "$2"\nprintf "%s" "$PLATFORM_FAMILY"',
                                    self.release(identifier, version, like))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, family)

    def test_old_or_unknown_distributions_fail_before_package_operations(self):
        for identifier, version in [('ubuntu', '18.04'), ('debian', '10'), ('centos', '7'),
                                    ('centos', '8'), ('rhel', '7.9'), ('amzn', '2'),
                                    ('opensuse-leap', '15.5'), ('alpine', '3.22'), ('unknown', '1')]:
            with self.subTest(identifier=identifier, version=version):
                result = self.shell('platform_detect "$2"', self.release(identifier, version))
                self.assertNotEqual(result.returncode, 0)

    def test_el8_prefers_separate_python_packages(self):
        result = self.shell('platform_detect "$2"\nprintf "%s" "${PYTHON_PACKAGES[*]}"', self.release('rocky', '8.10'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'python3.12 python3.11 python39')

    def test_selection_skips_incompatible_python_without_modifying_it(self):
        old, new = self.root / 'python-old', self.root / 'python-new'
        old.write_text('#!/bin/sh\nexit 1\n')
        new.write_text('#!/bin/sh\nexit 0\n')
        old.chmod(0o755)
        new.chmod(0o755)
        result = self.shell('select_python "$2" "$3"\nprintf "%s" "$PYTHON_BIN"', str(old), str(new))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(new))
        self.assertEqual(old.read_text(), '#!/bin/sh\nexit 1\n')

    def test_unit_uses_selected_interpreter_and_fixed_config(self):
        result = self.shell('PYTHON_BIN=/usr/bin/python3.12\nwrite_service_units "$2"', str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = (self.root / 'traffic-scheduler.service').read_text()
        self.assertIn('ExecStart=/usr/bin/python3.12 -u /usr/local/bin/traffic-scheduler --config /etc/traffic-scheduler/config.json tick', unit)
        self.assertIn('User=traffic-scheduler', unit)
        self.assertIn('OnCalendar=*-*-* *:*:00 Asia/Shanghai', (self.root / 'traffic-scheduler.timer').read_text())

    def test_standalone_installer_contains_same_platform_helpers(self):
        installer = (self.source / 'install.sh').read_text(encoding='utf-8')
        helpers = (self.source / 'installer_platform.sh').read_text(encoding='utf-8').strip()
        self.assertIn(helpers, installer)
        self.assertIn('sed -i "1c#!$PYTHON_BIN"', installer)


if __name__ == '__main__':
    unittest.main()
