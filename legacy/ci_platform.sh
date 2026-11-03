#!/usr/bin/env bash
# Runs only in a disposable root-owned distro container; never enables a timer.
set -Eeuo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive
cd /workspace
source ./installer_platform.sh
platform_detect

# Minimal images do not have a live init. Install tools solely to validate unit syntax.
case "$PLATFORM_FAMILY" in
    apt) apt-get update; apt-get install -y --no-install-recommends systemd ;;
    rpm) "$PACKAGE_MANAGER" install -y systemd ;;
    zypper) zypper --non-interactive refresh; zypper --non-interactive install --no-recommends systemd ;;
    pacman) pacman -Syu --noconfirm --needed systemd ;;
esac
original_python=''
if [[ -e /usr/bin/python3 ]]; then original_python=$(readlink -f /usr/bin/python3); fi
install_dependencies
check_system_tools
ensure_service_user
if [[ -n $original_python ]]; then [[ $(readlink -f /usr/bin/python3) == "$original_python" ]]; fi

install -d -m 0755 /usr/local/bin
install -m 0755 /workspace/traffic_scheduler.py /usr/local/bin/traffic-scheduler
sed -i "1c#!$PYTHON_BIN" /usr/local/bin/traffic-scheduler
install -d -m 0750 -o root -g traffic-scheduler /etc/traffic-scheduler
install -d -m 0700 -o traffic-scheduler -g traffic-scheduler /var/lib/traffic-scheduler
"$PYTHON_BIN" /usr/local/bin/traffic-scheduler configure --gb 0.01 --mbps 1 --region cn --builtin
[[ $(stat -c '%a' /etc/traffic-scheduler/config.json) == 640 ]]
runuser -u traffic-scheduler -- /usr/local/bin/traffic-scheduler status
write_service_units
systemd-analyze calendar '*-*-* *:*:00 Asia/Shanghai' >/dev/null
systemd-analyze verify /etc/systemd/system/traffic-scheduler.service /etc/systemd/system/traffic-scheduler.timer

test_dir=$(mktemp -d)
cp /workspace/traffic_scheduler.py /workspace/test_traffic_scheduler.py /workspace/test_installer.py \
   /workspace/installer_platform.sh /workspace/install.sh "$test_dir/"
cd "$test_dir"
"$PYTHON_BIN" -m unittest -v
echo "Platform checks passed: $PLATFORM_NAME; $PYTHON_BIN; no timer enabled."
