# Traffic Scheduler

Ubuntu 上全天随机分散执行的上下行流量任务。默认 **每天上行 20 GB、10 Mbps、大陆目标**，按北京时间计量。支持只上行、只下行、双向；上下行额度、限速、目标区域分别设置。

> 上行 = 本服务器向接收端上传数据。下行 = 本服务器从目标下载文件。下载镜像文件不会变成服务器上行。

## 一条命令安装

适用于正常启用 systemd 的 Ubuntu 20.04+，支持 x86_64、ARM64 等运行 Python 3 的架构。用 root 或有 sudo 权限的账户执行：

```bash
curl -fsSL https://raw.githubusercontent.com/KJ20051223/traffic-scheduler/main/install.sh -o install-traffic-scheduler.sh && sudo bash install-traffic-scheduler.sh
```

如果 Ubuntu 没装 curl，先执行 `sudo apt-get update && sudo apt-get install -y curl`。

安装器会交互询问：

1. 上行 / 下行 / 双向。
2. 每个启用方向的每日 GB 数和 Mbps 限速。
3. 每个方向使用大陆还是海外目标，或者输入自己的目标 URL。
4. 自动进行真实传输测试：**一定下载 10 MB**；启用了上行时再实际上传 10 MB。
5. 只有真实传输测试全部成功，才启用定时任务；失败时不显示安装成功、不启用任务。

全部使用默认值、无需交互：

```bash
sudo bash install-traffic-scheduler.sh --defaults
```

`--defaults` 在已有配置时保留已有设置，同样重新实测，不会清零当天额度。

## 手动上传脚本到服务器

从仓库下载 **[install.sh](https://raw.githubusercontent.com/KJ20051223/traffic-scheduler/main/install.sh)**，上传到服务器任意目录，在该目录执行：

```bash
sudo bash install.sh
```

这个 SH 内嵌完整 Python 程序，**不需要再上传压缩包、源码或配置文件**。安装仍需 Ubuntu 软件源提供 Python 3、证书和时区数据，不是离线系统依赖安装器。

## 内置目标

| 方向 | 大陆 | 海外（包括港澳台） |
| --- | --- | --- |
| 下行 | 华为云 mirrors / repo 镜像源，下载 Ubuntu Server ISO 的分块内容 | Cloudflare Speed Test 数据生成端点 |
| 上行 | 昆山杜克大学公开测速上传节点 | Cloudflare Speed Test 上传端点 |

大陆上传节点：`https://speedtest.dukekunshan.edu.cn:8080/upload`。2026-11-01 已在开发端和使用者 Ubuntu 服务器上分别得到 `size=1024` 的实际上传确认。开发端还验证了公网 IP `180.208.59.230` 归属 CN；运行时仍重新解析，不硬编码 IP。

**验证范围：** 开发端已通过大陆镜像 10 MB 下载、海外 10 MB 上传和 10 MB 下载。大陆上传仅确认了小请求；开发端 128 KiB 成功，1 MB 请求超时，尚未证明此节点可持续完成每日 20 GB。使用者服务器需要通过安装器的完整 10 MB 上传测试；失败会停止安装，不能把 1 KB 成功当作大流量稳定性保证。可以改选海外或使用自己的大陆接收端。

先前失败的华科镜像、拒绝公网的中科大测速端，以及已在使用者服务器上失败的运营商候选，不在默认列表中。程序使用 HTTP/1.1、明确的 User-Agent、禁用响应压缩、关闭连接，避免依赖旧 curl 请求行为；遇到空响应、403、5xx、错误网页或不遵守 Range 的镜像会视为失败并换源，不能保证任何公共端点永不限制访问。

连接前会要求目标 IPv4 为公网地址，通过 HTTPS 查询归属，再固定连接到该 IP，保留 HTTPS 的域名证书校验。选择大陆只允许 CN；选择海外只允许非 CN。遇到下载重定向会重新校验，不会跨区域降级冒充成功。归属库缓存 24 小时；IP 地理库可能存在误差，区域标签不保证物理机房或互联网路由位置。

对于本机代理返回的 `198.18.0.0/15` 等假 IP，程序会尝试 HTTPS DNS 查询公网地址，不改系统 DNS，不读取代理凭证。文件数据连接不使用 HTTP(S) 代理环境变量。

公共测速服务可能限制来源、请求大小或长期流量。脚本不绕过限制；如节点后来拒绝访问，日志会明确报错。可配置自己有权使用的接收端以提高长期稳定性。

## 随机时间与每日额度

- 每天北京时间 00:00 起按新日期计量，系统时区不变。
- 从全天的分钟中随机选取多个时间片，保存计划，分散执行；不再限制 03:00–05:00。
- 每个时间片最多工作至当分钟约第 52 秒，下载/上传按各自设置的速度交替执行，不同时叠加两份速度。
- 同一进程、定时任务和手动测试共用锁，避免并发叠加流量。
- 根据剩余字节数、实测速度和连接耗时留出执行余量；变慢时增加随机时段，时间紧张时使用剩余分钟补足。
- 到午夜停止当前日任务，次日重新产生计划；每天最多使用各方向配置的计量额度。
- 20 GB、10 Mbps 的理论有效传输时间至少是 **4 小时 26 分 40 秒**。安装当日如果剩余时间不足，网络过慢、断网或所有目标拒绝访问，不能保证当天刷足。状态和日志显示真实进度，不补记为成功。
- 为随机间隔、重试和元数据操作留余量，设置的理论传输总时间超过 16.8 小时/天时会拒绝配置。

**计量定义：** 1 GB = 1,000,000,000 字节，1 MB = 1,000,000 字节；10 Mbps = 1,250,000 字节/秒。软件限速约束有效负载读取/发送节奏，不是网卡瞬时峰值保证。TCP/TLS/HTTP 开销、ACK、重传和 IP/DNS 查询不含在配额里，运营商账单会不同。

首次和手动实测**额外计量**，默认仅下行模式消耗 10 MB，下行+上行测试消耗约 20 MB 的有效负载，且不改变当天正式额度。

每次最多请求 2 MiB，下载写入匿名临时文件并立即清理；上行在内存生成合成数据，不会读取、上传服务器现有文件。断电或 `kill -9` 时，未结算块按最多 2 MiB 保守计入，避免重启后超额。失败请求中已经发送/收到的有效负载也计入额度；`status` 分别展示计量量和已确认完成量。

## 日常命令

```bash
# 随时真正下载/上传测试，默认每方向 10MB；不受随机时间限制
sudo -u traffic-scheduler traffic-scheduler test --mb 10

# 更大的一次测试（每个测试方向 100MB，使用原配置的限速与区域）
sudo -u traffic-scheduler traffic-scheduler test --mb 100

# 今日进度、上下行独立额度、速度、区域和下一随机时段
sudo traffic-scheduler status

# 下一次分钟检查时间；它是检查时间，实际传输只在选中的随机时段进行
systemctl list-timers traffic-scheduler.timer

# 日志
sudo journalctl -u traffic-scheduler.service -n 80 --no-pager
sudo journalctl -u traffic-scheduler.service -f

# 停用（同时停止本次传输）
sudo systemctl disable --now traffic-scheduler.timer
sudo systemctl stop traffic-scheduler.service

# 恢复
sudo systemctl enable --now traffic-scheduler.timer
```

修改配置，推荐重新执行安装脚本，保留当天进度并重新实测后启用：

```bash
sudo bash install-traffic-scheduler.sh
```

也可手动编辑 `/etc/traffic-scheduler/config.json`，但需要先停止 timer/service，改完再以服务用户执行 `test`；当前配置未实测通过时 `tick` 会拒绝运行。不要删除当天 `quota-YYYY-MM-DD.json` 来调整设置，否则会丢失计量记录。

## 自定义目标与可选自建接收端

在交互设置时输入 URL 会替代该方向的内置列表。也可以在配置中的 `targets` 添加多个目标：

```json
[
  {"name": "自己的大陆接收端", "kind": "receiver", "url": "http://你的接收服务器公网IP:8765/upload"}
]
```

`post` 接受 HTTP 200/201/204 的非 HTML 接收端；Ookla 风格的 `size=N` 会核对字节数。`receiver` 要求 JSON `received_bytes` 与实际发送一致。`range` 为 ISO 等支持 206 的文件；`stream` 为自定义有界读取的二进制响应；`generated` 为 Cloudflare 风格 `?bytes=N` 端点。

仓库附 `receiver.py`，在另一台你拥有的大陆或海外服务器运行：

```bash
sudo python3 receiver.py --allow-ip 发送方服务器公网IPv4 --port 8765
```

仅允许显式指定的客户端 IPv4；接收数据直接丢弃，不写磁盘。你需要让自己的安全组允许该来源访问 8765/TCP。这个最简接收端的 HTTP 仅传输合成测试字节，不传账号、口令或现有文件；如配置 HTTPS 域名，可在前面使用自己的 TLS 反向代理。建议生产环境由 systemd 管理接收端。

## 卸载与旧版迁移

```bash
# 停止并移除程序、timer、service，保留配置和进度
sudo bash install-traffic-scheduler.sh --uninstall

# 连同本程序的配置和进度一并移除
sudo bash install-traffic-scheduler.sh --uninstall --purge
```

安装新版会停止并移除旧 `cn-traffic.timer`、`cn-traffic.service` 和旧可执行程序，防止双任务叠加。旧 `/etc/cn-traffic.json`、`/var/lib/cn-traffic/` 和旧服务账户不会自动删除，也不会将旧版下行计数误作新版上行计数。

## 开发与验证

```bash
python3 -m unittest -v
bash -n install.sh
```

测试包含真实本地 HTTP 收发、上传接收确认、部分失败计数、限额尾块、随机计划、双向独立计量、重启预留恢复、区域限制、假 IP DNS 回退、限速节奏和临时文件清理。CI 在 Linux/Python 3.8 与 3.12 下运行；Windows 本地不运行 fcntl 专属测试。

网络验证只进行小流量实测，未用第三方节点试跑 20 GB。首次安装会在你的目标 Ubuntu 上验证 systemd 文件和实际连通性。公网节点的可用性不是永久承诺。

参考：[Cloudflare Speed Test 接口](https://github.com/cloudflare/speedtest)、[systemd.timer](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)、[IP 归属接口](https://ipwhois.io/documentation)。
