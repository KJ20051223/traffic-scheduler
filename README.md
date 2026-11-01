# Traffic Scheduler 2.1.0

Ubuntu **仅下行**流量调度：每天按北京时间随机分散下载，默认 **20 GB/天、5 Mbps、中国大陆**。额度、速度、地区和自定义下载地址均可设置。已移除上传任务、上传节点和方向选择。

## 安装或升级

适用于启用 systemd 的 Ubuntu 20.04+、Python 3.8+。已有安装直接覆盖升级，无需卸载。

下面明确设置 **每日下行 20 GB、最高 5 Mbps、大陆内置源**；把 `20` 和 `5` 改成需要的数字：

```bash
curl -fsSL https://raw.githubusercontent.com/KJ20051223/traffic-scheduler/main/install.sh -o install-traffic-scheduler.sh && sudo bash install-traffic-scheduler.sh --gb 20 --mbps 5 --region cn --builtin
```

不带参数则交互设置，仅询问下行 GB、Mbps、地区和下载 URL：

```bash
sudo bash install-traffic-scheduler.sh
```

没有 curl 时先执行 `sudo apt-get update && sudo apt-get install -y curl`。安装仍需 Ubuntu 软件源安装 Python、证书和时区数据。

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
| 大陆 `cn` | 华为云 mirrors / repo | Ubuntu Server ISO 的部分字节，不安装、不执行 |
| 海外 `overseas`（含港澳台） | Cloudflare Speed Test | 测速用二进制数据 |

先前失败的华科下载地址和所有上传节点均已移除。空响应、错误网页、403、5xx 或不遵守 Range 会拒绝并换源。

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

# 随时真实下载 10 MB，使用保存的限速和地区，内容自动清理
sudo -u traffic-scheduler traffic-scheduler test --mb 10

# 日志与每分钟检查时间
sudo journalctl -u traffic-scheduler.service -n 80 --no-pager
systemctl list-timers traffic-scheduler.timer

# 停用，包括正在执行的下载
sudo systemctl disable --now traffic-scheduler.timer
sudo systemctl stop traffic-scheduler.service
```

修改推荐重新运行安装器，明确传参数，它会停止任务、保存、实测再激活。已安装 2.1.0 也可：

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

CI 在 Ubuntu/Python 3.8、3.12 覆盖配置迁移、交互和命令行保存、实际请求额度与限速参数、随机计划、日期切换、区域校验、失败换源、并发锁、崩溃预留和匿名文件清理。开发端仅做小流量联网实测，未用公共源持续下载 20 GB。
