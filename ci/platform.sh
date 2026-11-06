#!/bin/sh
set -eu
mkdir -p /tmp/toolkit-suite
cp -R /source/. /tmp/toolkit-suite/
cd /tmp/toolkit-suite
# Test-only EOL fixture setup. Production bootstrap NEVER changes apt sources.
# Bullseye's standard security pool has been archived; package signatures remain required.
if [ "$(. /etc/os-release; printf '%s' "$ID:$VERSION_ID")" = debian:11 ]; then
    printf '%s\n' \
      'deb http://archive.debian.org/debian bullseye main' \
      'deb http://archive.debian.org/debian-security bullseye-security main' > /etc/apt/sources.list
fi
sh ./install.sh --no-wizard
vps-toolkit --version
vps-toolkit traffic status
python_bin=$(sed -n 's/^exec "\([^"]*\)".*/\1/p' /usr/local/bin/vps-toolkit)
"$python_bin" -m unittest -v tests.test_toolkit tests.test_ratio tests.test_menu tests.test_agent_batch tests.test_progress tests.test_downloader compat.test_traffic_scheduler
"$python_bin" -c 'import json; c=json.load(open("/etc/vps-toolkit/config.json")); assert c["traffic"]["enabled"] is False; assert c["traffic"]["authorized_urls"] == []'
# Repeat offline installation without changing task state or requiring GitHub.
sh ./install.sh --offline --no-wizard
test -x /usr/local/bin/traffic-scheduler
test -x /usr/local/bin/pf
printf 'PLATFORM PASS: '
cat /etc/os-release
