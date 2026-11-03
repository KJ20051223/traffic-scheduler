#!/bin/sh
# VPS Toolkit standalone bootstrap. Payload is embedded by build.py.
set -eu
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
offline=0
no_wizard=0
for argument in "$@"; do
    case "$argument" in
        --offline) offline=1 ;;
        --no-wizard) no_wizard=1 ;;
        *) echo '用法: sudo sh install-vps-toolkit.sh [--offline] [--no-wizard]' >&2; exit 2 ;;
    esac
done
[ "$(id -u)" = 0 ] || { echo '请用 root/sudo 运行' >&2; exit 1; }
[ "$(uname -s)" = Linux ] || { echo '仅支持 Linux' >&2; exit 1; }
# shellcheck disable=SC1091
. /etc/os-release
if [ "${ID:-}" = debian ] && [ "${VERSION_ID:-}" = 11 ]; then
    echo '注意：Debian 11 已结束标准 LTS。软件源可能需管理员迁移到官方归档；本工具不会自动改源。' >&2
fi
select_python() {
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3.8; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import ssl,fcntl,sys;sys.exit(sys.version_info<(3,8))' 2>/dev/null; then
            python_bin=$(command -v "$candidate")
            return 0
        fi
    done
    return 1
}
if [ "$offline" = 0 ]; then
    export DEBIAN_FRONTEND=noninteractive
    case "${ID:-} ${ID_LIKE:-}" in
        *alpine*) apk add --no-cache python3 ca-certificates tzdata bash util-linux shadow iproute2 ;;
        *debian*|*ubuntu*) apt-get update; apt-get install -y --no-install-recommends python3 ca-certificates tzdata bash util-linux passwd iproute2 ;;
        *opensuse*|*suse*) zypper --non-interactive install python3 ca-certificates timezone bash util-linux shadow iproute2 ;;
        *arch*) pacman -S --needed --noconfirm python ca-certificates tzdata bash util-linux shadow iproute2 ;;
        *)
            if command -v dnf >/dev/null 2>&1; then manager=dnf
            elif command -v yum >/dev/null 2>&1; then manager=yum
            else echo '未识别包管理器，可先安装 Python 3.8+ 后使用 --offline' >&2; exit 1; fi
            "$manager" install -y ca-certificates tzdata bash util-linux shadow-utils iproute
            if ! select_python; then
                for package in python3.12 python3.11 python39 python3; do
                    if "$manager" install -y "$package" && select_python; then break; fi
                done
            fi
            ;;
    esac
fi
select_python || { echo '需要含 ssl/fcntl 的 Python 3.8+；不会替换系统 Python 或升级 libc。' >&2; exit 1; }
if ! id traffic-scheduler >/dev/null 2>&1; then
    command -v useradd >/dev/null 2>&1 || { echo '需要 useradd/groupadd（shadow/passwd 软件包）' >&2; exit 1; }
    "$python_bin" -c 'import grp;grp.getgrnam("traffic-scheduler")' >/dev/null 2>&1 || groupadd --system traffic-scheduler
    useradd --system --gid traffic-scheduler --no-create-home --home-dir /nonexistent --shell /sbin/nologin traffic-scheduler
fi
workdir=$(mktemp -d)
trap 'rm -rf -- "$workdir"' EXIT HUP INT TERM
# The generated installer verifies this payload before extracting or executing it.
__EMBEDDED_PAYLOAD__
if [ "$no_wizard" = 1 ]; then
    "$python_bin" "$workdir/install.py" --no-wizard
else
    "$python_bin" "$workdir/install.py"
fi
