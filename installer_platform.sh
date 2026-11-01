# Platform helpers embedded in the standalone installer; also exercised in distro containers.
platform_detect() {
    local release_file=${1:-/etc/os-release}
    local ID='' ID_LIKE='' VERSION_ID='' PRETTY_NAME=''
    [[ -r $release_file ]] || { echo '无法读取 /etc/os-release，无法识别发行版。' >&2; return 1; }
    . "$release_file"
    PLATFORM_ID=$ID
    PLATFORM_NAME=${PRETTY_NAME:-$ID}
    PLATFORM_VERSION=$VERSION_ID
    PLATFORM_FAMILY=''
    local major=${VERSION_ID%%.*}
    case "$ID" in
        ubuntu)
            [[ $major =~ ^[0-9]+$ && $major -ge 20 ]] || { echo '需要 Ubuntu 20.04+' >&2; return 1; }
            PLATFORM_FAMILY=apt ;;
        debian)
            [[ $major =~ ^[0-9]+$ && $major -ge 12 ]] || { echo '需要 Debian 12+，旧版本请先处理系统和软件源升级。' >&2; return 1; }
            PLATFORM_FAMILY=apt ;;
        rhel|rocky|almalinux|ol)
            [[ $major =~ ^[0-9]+$ && $major -ge 8 ]] || { echo '需要 RHEL/Rocky/AlmaLinux/Oracle Linux 8+' >&2; return 1; }
            PLATFORM_FAMILY=rpm ;;
        centos)
            [[ $major =~ ^[0-9]+$ && $major -ge 9 ]] || { echo '需要 CentOS Stream 9+；不支持 CentOS 7/8。' >&2; return 1; }
            PLATFORM_FAMILY=rpm ;;
        fedora)
            [[ $major =~ ^[0-9]+$ && $major -ge 40 ]] || { echo '需要 Fedora 40+' >&2; return 1; }
            PLATFORM_FAMILY=rpm ;;
        amzn)
            [[ $major =~ ^[0-9]+$ && $major -ge 2023 ]] || { echo '需要 Amazon Linux 2023，不支持 Amazon Linux 2。' >&2; return 1; }
            PLATFORM_FAMILY=rpm ;;
        opensuse-leap)
            [[ $major =~ ^[0-9]+$ ]] || return 1
            if (( major < 15 )) || [[ $major == 15 && ${VERSION_ID#*.} -lt 6 ]]; then
                echo '需要 openSUSE Leap 15.6+' >&2; return 1
            fi
            PLATFORM_FAMILY=zypper ;;
        opensuse-tumbleweed|opensuse-slowroll) PLATFORM_FAMILY=zypper ;;
        arch|manjaro|endeavouros) PLATFORM_FAMILY=pacman ;;
        *)
            case " $ID_LIKE " in
                *' debian '*|*' ubuntu '*) PLATFORM_FAMILY=apt ;;
                *' rhel '*|*' fedora '*|*' centos '*) PLATFORM_FAMILY=rpm ;;
                *' suse '*|*' opensuse '*) PLATFORM_FAMILY=zypper ;;
                *' arch '*) PLATFORM_FAMILY=pacman ;;
                *) echo "暂不支持发行版 ${ID:-未知}；需要 systemd 和 apt/dnf/yum/zypper/pacman。" >&2; return 1 ;;
            esac
            echo "衍生发行版 $PLATFORM_NAME 按 $PLATFORM_FAMILY 适配，仍将检查实际依赖。" >&2
            ;;
    esac
    PYTHON_PACKAGES=(python3 python3.12 python3.11)
    case "$PLATFORM_FAMILY" in
        apt) PACKAGE_MANAGER=apt-get ;;
        rpm)
            if command -v dnf >/dev/null; then PACKAGE_MANAGER=dnf; else PACKAGE_MANAGER=yum; fi
            if [[ $major == 8 ]]; then PYTHON_PACKAGES=(python3.12 python3.11 python39); fi
            ;;
        zypper)
            PACKAGE_MANAGER=zypper
            PYTHON_PACKAGES=(python3 python313 python312 python311)
            ;;
        pacman) PACKAGE_MANAGER=pacman; PYTHON_PACKAGES=(python) ;;
    esac
}

select_python() {
    local candidate
    local candidates=(/usr/bin/python3 /usr/local/bin/python3
        /usr/bin/python3.14 /usr/bin/python3.13 /usr/bin/python3.12 /usr/bin/python3.11
        /usr/bin/python3.10 /usr/bin/python3.9 /usr/bin/python3.8)
    if [[ $# -gt 0 ]]; then candidates=("$@"); fi
    for candidate in "${candidates[@]}"; do
        if [[ -x $candidate ]] && "$candidate" -c 'import sys, ssl, fcntl; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
            PYTHON_BIN=$candidate
            return 0
        fi
    done
    return 1
}

install_python_package() {
    case "$PLATFORM_FAMILY" in
        apt) apt-get install -y --no-install-recommends "$1" ;;
        rpm) "$PACKAGE_MANAGER" install -y "$1" ;;
        zypper) zypper --non-interactive install --no-recommends "$1" ;;
        pacman) pacman -S --needed --noconfirm "$1" ;;
    esac
}

install_dependencies() {
    command -v "$PACKAGE_MANAGER" >/dev/null || { echo "找不到包管理器 $PACKAGE_MANAGER" >&2; return 1; }
    case "$PLATFORM_FAMILY" in
        apt)
            apt-get update
            apt-get install -y --no-install-recommends ca-certificates tzdata util-linux passwd
            ;;
        rpm) "$PACKAGE_MANAGER" install -y ca-certificates tzdata util-linux shadow-utils ;;
        zypper)
            zypper --non-interactive refresh
            zypper --non-interactive install --no-recommends ca-certificates timezone util-linux shadow
            ;;
        pacman)
            # No -Sy partial upgrade, and no unsolicited full-system upgrade.
            pacman -S --needed --noconfirm ca-certificates tzdata util-linux shadow || {
                echo 'Arch 软件源或数据库不可用，请先完成系统更新（pacman -Syu）后重试。' >&2; return 1;
            }
            ;;
    esac
    if ! select_python; then
        local package
        for package in "${PYTHON_PACKAGES[@]}"; do
            if install_python_package "$package" && select_python; then break; fi
        done
    fi
    select_python || { echo '未找到支持 ssl/fcntl 的 Python 3.8+；请检查当前系统的软件源。' >&2; return 1; }
    echo "系统: $PLATFORM_NAME | 包管理器: $PACKAGE_MANAGER | Python: $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"
}

check_system_tools() {
    local tool version
    for tool in systemctl systemd-analyze runuser getent useradd groupadd usermod install sed chown chmod; do
        command -v "$tool" >/dev/null || { echo "缺少系统工具: $tool" >&2; return 1; }
    done
    version=$(systemd-analyze --version)
    version=${version#systemd }
    version=${version%% *}
    version=${version%%$'\n'*}
    [[ $version =~ ^[0-9]+$ && $version -ge 239 ]] || { echo '需要 systemd 239 或更新版本。' >&2; return 1; }
}

ensure_service_user() {
    if ! getent group traffic-scheduler >/dev/null; then groupadd --system traffic-scheduler; fi
    local nologin_bin
    nologin_bin=$(command -v nologin) || { echo '缺少 nologin 工具' >&2; return 1; }
    if ! id -u traffic-scheduler >/dev/null 2>&1; then
        useradd --system --gid traffic-scheduler --no-create-home --home-dir /nonexistent --shell "$nologin_bin" traffic-scheduler
    else
        [[ $(id -u traffic-scheduler) != 0 ]] || { echo 'traffic-scheduler 账户不能为 root' >&2; return 1; }
        usermod --append --groups traffic-scheduler traffic-scheduler
    fi
}

write_service_units() {
    local unit_dir=${1:-/etc/systemd/system}
    cat > "$unit_dir/traffic-scheduler.service" <<TRAFFIC_SERVICE
[Unit]
Description=Random daily download-only traffic budget
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=traffic-scheduler
Group=traffic-scheduler
ExecStart=$PYTHON_BIN -u /usr/local/bin/traffic-scheduler --config /etc/traffic-scheduler/config.json tick
RuntimeMaxSec=57s
TimeoutStopSec=5s
StateDirectory=traffic-scheduler
StateDirectoryMode=0700
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
ReadWritePaths=/var/lib/traffic-scheduler
StandardOutput=journal
StandardError=journal
TRAFFIC_SERVICE
    cat > "$unit_dir/traffic-scheduler.timer" <<'TRAFFIC_TIMER'
[Unit]
Description=Check randomly selected transfer slices every minute

[Timer]
OnCalendar=*-*-* *:*:00 Asia/Shanghai
AccuracySec=1s
RandomizedDelaySec=0
Persistent=false
Unit=traffic-scheduler.service

[Install]
WantedBy=timers.target
TRAFFIC_TIMER
}

restore_service_contexts() {
    if command -v restorecon >/dev/null; then
        restorecon -RF /usr/local/bin/traffic-scheduler /etc/traffic-scheduler /var/lib/traffic-scheduler \
            /etc/systemd/system/traffic-scheduler.service /etc/systemd/system/traffic-scheduler.timer
    fi
}
