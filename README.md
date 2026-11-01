# Traffic Scheduler 2.2.0

Linux **仅下行**流量调度：每天按北京时间随机分散下载，默认 **20 GB/天、5 Mbps、中国大陆**。额度、速度、地区和自定义下载地址均可设置。已移除上传任务、上传节点和方向选择。

## 安装或升级

适用于运行 **systemd 239+** 的以下 Linux 系统，安装器自动安装依赖并选择 **Python 3.8+**。已有安装直接覆盖升级，无需卸载，保留配置及当天计数。

| 系统 | 版本范围 | 包管理器 |
| --- | --- | --- |
| Ubuntu | 20.04+ | apt |
| Debian | 12+ | apt |
| RHEL、Rocky、AlmaLinux、Oracle Linux | 8+，软件源需提供 Python 3.8+ | dnf / yum |
| CentOS Stream | 9+ | dnf |
| Fedora | 40+ | dnf |
| Amazon Linux | 2023 | dnf |
| openSUSE | Leap 15.6+、Tumbleweed / Slowroll | zypper |
| Arch Linux | 滚动发行版，系统已正常更新 | pacman |

Linux Mint、Manjaro 等衍生系统会根据 `/etc/os-release` 的 `ID_LIKE` 选择相应安装方式，仍会检查实际工具和解释器。Alpine 默认 OpenRC、Debian 11 及以前、CentOS 7/8、Amazon Linux 2、未运行 systemd 的普通容器、Windows/macOS 不在当前支持范围。

系统自带的软件源必须可用，安装器不会修改软件源、关闭 SELinux、升级整个系统或替换 `/usr/bin/python3`。RHEL 8 等默认 Python 较旧的系统会尝试安装独立的 Python 3.12 / 3.11 / 3.9；CLI、安装实测和 systemd 使用同一选定解释器。Arch 不执行 `pacman -Sy` 部分升级，软件源失效时提示用户先完成系统更新。

下面明确设置 **每日下行 20 GB、最高 5 Mbps、大陆内置源**；把 `20` 和 `5` 改成需要的数字：

```bash
curl -fsSL https://raw.githubusercontent.com/KJ20051223/traffic-scheduler/main/install.sh -o install-traffic-scheduler.sh && sudo bash install-traffic-scheduler.sh --gb 20 --mbps 5 --region cn --builtin
```

不带参数则交互设置，仅询问下行 GB、Mbps、地区和下载 URL：

```bash
sudo bash install-traffic-scheduler.sh
```

没有 curl 时，可先按系统安装：Debian/Ubuntu 使用 `apt-get update && apt-get install -y curl`；RPM 系使用 `dnf install -y curl`；openSUSE 使用 `zypper install curl`；Arch 使用 `pacman -S --needed curl`。以上软件安装命令须 root 或 sudo 权限。也可直接使用下面的手动上传方式。安装仍需系统软件源提供 Python、证书、时区及用户管理工具。

### 手动上传服务器

下载仓库里的 **[install.sh](https://raw.githubusercontent.com/KJ20051223/traffic-scheduler/main/install.sh)**，上传服务器，在该目录执行：

```bash
sudo bash install.sh --gb 20 --mbps 5 --region cn --builtin
```

SH 内嵌完整程序，**只上传这一个文件即可**，不需要压缩包或单独上传 Python。

### 配置生效与旧版迁移

- 安装器停止旧任务，写入 `/etc/traffic-scheduler/config.json`，保存后回读显示实际 GB、Mbps、地区。
- 安装实测和 systemd 服务明确读取同一配置路径；任务日志也打印实际额度和限速。
- 用刚保存的限速和地区进行 **10 MB 真实下载**，成功后才启用 timer；失败保持停用。交互安装可重新设置或重试。
- 保留当天已计量的下行进度。降低额度到已下载量以下时，当天停止继续下载。
- `--defaults` 保留已有参数并迁移为仅下行；首次安装才使用默认 20 GB、5 Mbps。要覆盖旧参数，请明确传 `--gb` 和 `--mbps`。
- 从 2.0.x 升级：已启用下行则保留下行参数；原来仅上行则把**当时启用的额度、速度、地区**迁移到下行，上传 URL 改为对应地区内置下载源。不会把闲置下行的 20 GB/10 Mbps 当成原设置。

## 下载文件会自动删除吗？

**会，不会留下完整 ISO 或不断积累文件。** 每次最多请求 **2 MiB**，写入 Linux 匿名临时文件。文件创建时就没有目录链接，正常完成、失败或中断时关闭；进程被强制结束时也由内核回收。

首次 10 MB 实测分块执行，测试目录在结束或异常退出时清理。断电或强杀即便留下测试状态目录，也没有可见下载内容。配置、计量 JSON 和日志是运行记录，不是下载文件。

Linux CI 检查正常完成、部分失败和中断后的清理，还检查**下载进行中临时文件链接数为 0**。

## 内置目标

| 区域 | 内置源 | 下载内容 |
| --- | --- | --- |
| 大陆 `cn` | 南京大学、中科大、清华、北外、浙大、中科院软件所（6 个） | Ubuntu Server ISO 的部分字节，不安装、不执行 |
| 海外 `overseas`（含港澳台） | Cloudflare Speed Test | 测速用二进制数据 |

先前失败的华科、用户收到 418/403 的华为云地址和所有上传节点均已移除。华为云开发端偶尔仍可用，用户端失败的确切原因尚未确认，不能把同一运营商的两个域名视作独立备份。阿里云官方镜像目录公告暂停公网 ISO 下载，也未加入列表。

### 每次随机选择与自动重试

- 每次真实测试、每个调度时段都重新打乱全部候选的顺序，从可用候选中随机开始；本次源能正常下载就继续用它。下一时段重新随机，随机结果允许重复。
- 第一个失败后自动切换其他候选，不要求先把所有源逐个测试一遍；都保持同样的地区校验和限速。
- 超时、连接重置/断连、HTTP 408/425/429/5xx 等临时错误，每源每时段最多尝试 **3 次**（含首次），受时段截止和剩余额度限制。优先换源；再次尝试前分别等待约 **2 秒、5 秒**，加最多 1 秒随机间隔。
- 重试耗尽后该源冷却 **5 分钟**；403、418 等明确拒绝不会当场连续重试，而是跳过并冷却 **30 分钟**。
- 尊重 `Retry-After` 秒数或 HTTP 日期；等待超过 15 秒时留到后续时段尝试，不占住当前任务长期等待。
- 冷却状态保存到 `/var/lib/traffic-scheduler/source-health.json`，后续进程继续使用。全部不可用时记录错误；运行中的 timer 后续随机时段会再尝试。**首次安装实测失败仍保持 timer 停用，需要实测成功后再启用**，不会自行跳过验证。

开发端对这 6 个源各进行了 64 KiB Range 探测和 10 MB / 5 Mbps 下载实测；实际 IP 均查询为 CN。开发端通过不代表所有来源均可访问，也未验证长期 20 GB。你的服务器会重新随机选源实测。

大陆只连接查询归属为 CN 的公网 IPv4；海外只允许非 CN。连接固定到已校验 IP，保留 HTTPS 证书校验，重定向重新校验地区，不跨区域回退。归属缓存 24 小时，地理库可能有误差，不能保证路由或机房位置。

公网数据连接不使用代理环境变量。系统 DNS 返回假 IP 时尝试 HTTPS DNS，不改系统 DNS。公共源可能限制来源或长期流量，首次安装会在你的服务器实测，不能保证持续可用。

自定义目标应是可访问的二进制文件 URL，可交互输入或：

```bash
sudo bash install-traffic-scheduler.sh --gb 12 --mbps 3 --region cn --url 'https://你的域名/文件.bin'
```

更换地区清除旧地区自定义地址；显式指定 `--url` 使用新地址，`--builtin` 恢复内置源，两者不能同时使用。

## 随机时间与计量

- 按北京时间日期计量，不改系统时区。每天从全天分钟中随机选多个时间片，保存计划，间歇执行。
- 每个时段最多到该分钟约第 52 秒，任务与手动测试共用锁，避免重叠。
- 根据剩余额度、设置速度和实测速率分配时段；变慢时增加随机时段，时间不足时使用剩余分钟尽力补足。
- 午夜结束当日任务，次日重新随机。安装当日剩余时间不足、网络慢或源不可用，不能保证刷足，进度不会虚报。
- 20 GB 在 5 Mbps 下理论至少需 **8 小时 53 分 20 秒**有效传输。理论时间超过 16.8 小时/天会拒绝配置，给间隔和连接留余量。

1 GB=10^9 字节，1 MB=10^6 字节，1 Mbps=125,000 字节/秒。限速控制有效负载读取节奏，受 TCP 缓冲影响，**不等于网卡瞬时峰值保证**。TCP/TLS/HTTP 开销、ACK、重传、DNS/IP 查询不包含在额度内；仅下行也有必要请求和 ACK 上行。

失败中已收到的有效负载也计入额度，完整确认量另外显示。崩溃时未结算预留最多 2 MiB 保守计入，避免重启超额。首次/手动实测额外计量，不扣正式额度，换源可能增加测试用量。修改配置不会清零当天计数。

## 日常命令

```bash
# 实际配置和今日进度
sudo traffic-scheduler status
traffic-scheduler --version

# 所有当前目标、冷却剩余时间及上次错误
sudo traffic-scheduler sources

# 随时真实下载 10 MB，使用保存的限速和地区，内容自动清理
sudo -u traffic-scheduler traffic-scheduler test --mb 10

# 日志与每分钟检查时间
sudo journalctl -u traffic-scheduler.service -n 80 --no-pager
systemctl list-timers traffic-scheduler.timer

# 停用，包括正在执行的下载
sudo systemctl disable --now traffic-scheduler.timer
sudo systemctl stop traffic-scheduler.service
```

修改推荐重新运行安装器，明确传参数，它会停止任务、保存、实测再激活。已安装 2.2.0 也可：

```bash
sudo systemctl disable --now traffic-scheduler.timer
sudo systemctl stop traffic-scheduler.service
sudo traffic-scheduler configure --gb 12 --mbps 3 --region cn --builtin && sudo -u traffic-scheduler traffic-scheduler test --mb 10 && sudo systemctl enable --now traffic-scheduler.timer
```

不传参数的 `traffic-scheduler configure` 为交互设置。改变后必须重新实测，未经验证的配置无法调度；不要删除当天 `quota-YYYY-MM-DD.json` 来修改设置。

## 卸载

```bash
# 保留配置及计量
sudo bash install-traffic-scheduler.sh --uninstall

# 同时清除本程序配置和计量
sudo bash install-traffic-scheduler.sh --uninstall --purge
```

升级停用旧 `cn-traffic.timer/service`，防止双任务叠加；旧 `/etc/cn-traffic.json`、`/var/lib/cn-traffic/` 和账户不自动删除。

## 开发验证

```bash
python3 -m unittest -v
bash -n install.sh
```

CI 在 Python 3.8、3.12 运行核心测试，另用 Ubuntu、Debian、Rocky、AlmaLinux、Oracle Linux、CentOS Stream、Fedora、Amazon Linux、openSUSE、Arch 的发行版容器检查依赖安装、解释器选择、账户与配置读取权限、systemd 单元语法，并运行真实本地 HTTP 测试。发行版容器不运行完整 init，不等于逐一整机启动验收；正式安装仍要求 systemd 正在运行，并在使用者服务器真实下载 10 MB 后才启用 timer。

测试覆盖配置迁移、交互和命令行保存、实际请求额度与限速参数、随机计划、日期切换、区域校验、源顺序随机、不修改内置表、失败换源、有间隔的有限重试、Retry-After、403/418 冷却、重启延续冷却、并发锁、崩溃预留和匿名文件清理。开发端仅做小流量联网实测，未用公共源持续下载 20 GB。

Python 安装依据：[RHEL](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/8/html/configuring_basic_system_settings/assembly_introduction-to-python_configuring-basic-system-settings)、[Amazon Linux 2023](https://docs.aws.amazon.com/linux/al2023/ug/python.html)。

源目录参考：[南京大学](https://mirrors.nju.edu.cn/ubuntu-releases/24.04/)、[中科大说明](https://mirrors.ustc.edu.cn/help/ubuntu-releases.html)、[阿里云公网 ISO 暂停公告](https://mirrors.aliyun.com/ubuntu-releases/24.04/)。
