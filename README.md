# VPS Toolkit

Linux VPS 命令行工具，合并原 traffic-scheduler 的定时下行、手动下载测试、PF/Realm 管理、云平台检测与客户机 Agent 审计。版本 **3.3.1**。

**升级保留已有下载任务的启停状态、原配置、当日统计与运行中的 Realm；新自动下载任务默认关闭。** 不修改 SSH、防火墙、安全组、系统 Python 或 libc，不新增网站、TLS 监听或远程管理端口。HTTPS 仅作为下载客户端使用。

## 安装与升级

联网服务器执行同一条命令安装或升级，已有任务不会被重新启用、停止或清零：

```sh
curl -fL --retry 3 --connect-timeout 10 https://raw.githubusercontent.com/KJ20051223/vps-toolkit/main/install.sh -o install-vps-toolkit.sh && sudo sh install-vps-toolkit.sh
```

国内服务器无法访问 GitHub 时，在本地下载仓库中的 [install.sh](install.sh)，**只上传这一个文件**，无需上传压缩包：

```sh
sudo sh ./install.sh
# 非交互安装，跳过向导：
sudo sh ./install.sh --no-wizard
# 已安装 Python 3.8+、CA、tzdata、bash、util-linux、账户管理工具时，可完全离线安装：
sudo sh ./install.sh --offline --no-wizard
```

单文件包含工具代码，并在解包前验证嵌入载荷 SHA256。Realm 二进制另行按官方固定版本、可信 SHA256 验证后安装；支持离线包。镜像站的测试文件不包含在安装包中。

```sh
sudo vps-toolkit          # 中文菜单
sudo vps-toolkit wizard   # 健康检查 → Agent 审计 → 可选模块 → 手动实测 → 可选调度 → 备份
sudo vps-toolkit health
sudo vps-toolkit cloud show
```

## 中文交互菜单（3.2.0）

执行 `sudo vps-toolkit` 进入数字菜单。选择功能后清屏进入独立结果页，以中文状态、用量和分组报告显示结果；**输入 `0` 才返回上一级**，不会执行完立即刷回菜单。主菜单输入 `0` 退出，兼容原 `19` 退出。

下载任务调度、Realm 规则和服务管理使用数字子菜单。操作失败也停留在结果页；无效选项会提示重新输入，Ctrl+C/输入结束可退出。规则修改、Agent 卸载、配置恢复等原有确认与权限检查保留。手动下载通过独立账户运行，其结果同样显示中文，不输出 JSON。

独立命令默认仍输出 JSON，方便已有脚本读取。终端直接运行命令时，可在模块名前加 `--human`：

```sh
sudo vps-toolkit --human health
sudo vps-toolkit --human traffic status
sudo vps-toolkit --human agent audit
```

清屏使用终端控制序列，仅在终端输出时启用；重定向到日志不会写入清屏字符。界面更新保留原任务状态、配置、计量和累计比例设置。

## 手动下载：保留，且与自动任务共享预算

先填写你有权测试的文件 URL。默认新配置没有授权资源，不能自动下载。旧安装会继承旧资源授权，不改变原任务状态。

```sh
sudo vps-toolkit traffic configure --daily-gb 10 --monthly-gb 300 --task-gb 0.1 --mbps 5 --max-runtime 300
sudo vps-toolkit traffic authorize 'https://你的已授权文件地址/large-file.bin'
sudo vps-toolkit traffic download --url 'https://你的已授权文件地址/large-file.bin' --mb 10
sudo vps-toolkit traffic status
```

`traffic test` 是 `traffic download` 的别名，默认 10 MB。可加 `--mbps 2 --max-runtime 120`，只能进一步降低配置上限；可加 `--sha256 完整摘要` 校验本次逻辑下载内容。URL 必须属于配置地区（默认大陆），可 `traffic configure --region overseas` 切换海外。

**下载内容流式读取，逐块计量并自动清理临时内容，不保存完整文件。** 正常完成、失败和中断都会执行清理；磁盘只保留配置、计量和任务状态。多块请求使用连续偏移，不反复读取文件开头；恢复需要源站支持 Range。HTTP 403/418、TLS 错误会明确报错，临时网络错误最多重试 3 次，不无限重试。没有承诺任何公共镜像全天可用。

```sh
sudo vps-toolkit traffic pause        # 暂停；在当前有限大小请求结束后生效
sudo vps-toolkit traffic resume       # 解除暂停
sudo vps-toolkit traffic stop         # 停止当前任务，不停止 Realm
sudo vps-toolkit traffic resume --job 任务编号  # 继续暂停/停止/中断任务的偏移
```

流量按 GB=10^9 字节、Mbps=10^6 比特/秒计量。所有新手动、新调度和受管理的兼容旧任务共用文件锁及每日/月预算；异常未结算预留按已消耗处理。协议开销、重传和其他应用流量不在有效负载统计中。要求完整 SHA256 的中断任务不能无文件恢复，需重新开始。

## 实时下载进度（3.3.0）

菜单 **10 下载任务调度 → 14 实时下载进度**，或主菜单 **11 流量统计**。每秒清屏刷新，输入 `0` 并回车返回，不会再多问一次返回。

```sh
sudo vps-toolkit traffic watch             # 终端实时页面，0 回车返回
sudo vps-toolkit --human traffic progress  # 中文单次快照
sudo vps-toolkit traffic progress          # JSON 单次快照
```

显示当前任务/兼容入口、已接收量、进度条、有效负载速度、设置限速、当前任务预计剩余时间、今日用量、剩余额度和整机累计比例缺口。兼容旧入口按当前分块显示百分比，另列全天用量。页面读取原子写入的 `progress.json`，不获取下载计量锁，下载运行中也可查看；不推进、清零或修改配额与累计网卡计量。

实际已接收量最多约每秒更新，当前分块尚未结算的字节会明确区分；最终计费上限仍按原配额结算。连接等待、服务端故障、重试和比例变化会影响完成时间，ETA 只估计当前逻辑任务。进程已退出时不会把旧速度显示为仍在下载。进度文件固定覆盖写入，不按刷新次数产生新文件；下载内容照常清理。

## 定时下载与修改规则

只修改预算不会改变任务启停，也不会清空今日统计：

```sh
sudo vps-toolkit traffic configure --daily-gb 10 --monthly-gb 300 --mbps 5 --hourly-weights estimated
sudo vps-toolkit traffic configure --window 00:00-23:59 --times ''
sudo vps-toolkit traffic enable
```

旧 traffic-scheduler 仍启用时，`enable` 会提示先确认替换；明确迁移用 `traffic enable --replace-legacy`，这才会停止旧定时入口并启用新入口。也可以继续保留旧任务，使用原 `traffic-scheduler` 命令独立操作；新统一预算和更低速度上限同时约束兼容旧任务。统一工具的自动任务仍保持关闭，避免双份调度。

默认随机分散，按速率与任务预算切块，每分钟最多一次、单次约 40 秒工作量；午夜有随机等待，错过的时段会在剩余窗口重新安排。`estimated` 为可调整的作息估算权重，**不是实测真实用户行为**。新配置默认均匀权重、系统时区；旧配置保留北京时间和权重。已有统计时禁止直接改日期时区，避免重复额度。

```sh
# 固定时间（替代随机时间）
sudo vps-toolkit traffic configure --times 09:20,13:45,20:10
# 恢复随机；或 --hourly-weights 后传逗号分隔的 24 个正数
sudo vps-toolkit traffic configure --times '' --hourly-weights uniform
# 假定链路 10 Mbps，为 Realm 留出至少 4 Mbps 的下载额度空间：
sudo vps-toolkit traffic configure --link-mbps 10 --reserve-mbps 4 --mbps 8
sudo vps-toolkit traffic disable
```

预留通过限制本工具下载速率实现，不是系统 QoS；不会修改 tc/防火墙或保证外部程序带宽。实际下载并发恒为 **1**，`--max-concurrency` 只设上限，不会启动多个抢占共享预算的下载。默认每任务 100 MB、最长 300 秒、每日 20 GB、每月 600 GB、5 Mbps。低速、窄窗口、资源故障或大额度可能导致当日无法完成，预算是上限，不是完成承诺。

## 按整机累计上行:下行比例补下载（3.1.0）

菜单 **10 → 7 下载额度与限速** 只修改日/月预算与限速，**保留当前固定额度/比例模式**；比例模式的额度是补下载上限，不是必须跑满的目标。菜单 **10 → 8** 设置比例并进入比例模式，**10 → 15 切换下载模式** 才显式切回固定额度或选择比例模式。比例支持 `1:2`、`１：２`、冒号两侧空格和常见粘贴隐藏字符，保存为统一格式；菜单在比例输入后立即校验，可重填或输入 `0` 返回。

实时进度新增 **本次任务还剩**、**达到比例还需补下载（含容差）**、**当前预算内最多可补**。后者为比例缺口与剩余日/月预算的较小值；比例满足时显示 0，不因预算还有剩余而继续补流。

首次进入比例模式、保存比例设置或打开实时进度时，自动读取当前出口网卡本次开机累计 TX/RX 并保存缺失的计量基线，无需手动输入。已有基线不重置；下载正在占用计量锁时先显示网卡实测预览，进度页每 5 秒非阻塞重试初始化，空闲后自动保存。网卡识别/读取失败才在比例设置中提示手动指定出口网卡；数据损坏或已有网卡发生变化时要求核对后校准，不擅自清零。

每秒刷新的进度快照本身保持只读，不结算下载配额；自动初始化仅在基线缺失时写一次计量记录。也可执行 `sudo vps-toolkit --human traffic status`（菜单 **10 → 1**）立即采样。网卡读不到时明确显示错误，不编造累计量。首次接入前已经重启丢失的历史无法从网卡恢复，可在 **10 → 9 补录 / 校准整机历史累计流量** 中输入有依据的累计量；后续继续自动计量。

升级会为识别到的旧 systemd 下载服务追加本工具的 `50-vps-toolkit.conf`，只增加 `/var/lib/vps-toolkit/traffic` 的写入权限，修复旧服务在 `ProtectSystem=strict` 下无法保存比例计量基线的问题。原服务命令、定时器、限速、启停状态与其他隔离设置保持；不重启正在执行的旧任务，权限在下次服务启动时生效。

读取出口网卡的全部 TX/RX，包括业务、转发和本工具产生的流量；按**累计上行:累计下行**计算，仅在下行不足时补下载。默认示例 `1:2`：累计上传 5 GB、下载 6 GB，目标下行 10 GB，最多还需补约 4 GB；已有下行足够就停止，不会为了固定每日额度继续下载。累计计量**不按日、月清零**，不会主动上传。

升级后明确切换策略，原额度、限速、地区、授权资源和任务启停状态保留：

```sh
sudo vps-toolkit traffic configure --strategy ratio --ratio 1:2 --ratio-schedule follow --ratio-interface auto
sudo vps-toolkit traffic status
# 已有任务仍会运行；若之前没有启用任何任务：
sudo vps-toolkit traffic enable
# 若想把仍启用的旧 traffic-scheduler 明确迁移到新调度器：
sudo vps-toolkit traffic enable --replace-legacy
```

`follow` 每分钟复核一次，有缺口才按约 40 秒工作量小块补下行，下载期间每块（不超过 2 MiB/约 8 秒限速工作量）重新读取计数；外部业务补足下行后，当前有限大小请求结束便停止。默认容差 5%，不足 1 MB 不启动，避免围绕目标不断发起微小请求。目标变化、并发业务、内核统计延迟和协议开销意味着比例是近似值；若下行已经过多，只能等待后续上行增长，无法把已有流量减掉。

```sh
# 比例可修改；10 Mbps 仍是峰值上限，日/月预算仍作为补下载上限
sudo vps-toolkit traffic configure --ratio 1:3 --mbps 10 --daily-gb 30 --monthly-gb 900
sudo vps-toolkit traffic configure --ratio-tolerance-pct 2
# 想保留原全天随机/作息权重分散补流，使用 random（跟随会更慢）
sudo vps-toolkit traffic configure --ratio-schedule random --times '' --window 00:00-23:59
# 实时跟随、全天允许检查（清除之前设置的固定时刻）
sudo vps-toolkit traffic configure --ratio-schedule follow --times '' --window 00:00-23:59
# 退出比例模式，恢复原固定额度随机调度
sudo vps-toolkit traffic configure --strategy quota
```

日/月预算仅统计本工具有效负载，整机比例使用网卡计数，二者用途不同。缺口大于剩余预算或上传增长超过补下载能力时，本工具会保持限速与预算，无法保证达到目标。`traffic status` 的 `machine_traffic` 显示累计上下行、目标、缺口、接口和历史完整性提示。兼容旧定时入口也执行比例上限；`follow` 时按分钟检查，不再等待旧随机时段，但旧入口自己的额度上限仍生效。要统一时间窗口等新调度设置，请显式迁移到新入口。

### 首次历史接入、重启与网卡选择

首次使用读取**本次开机以来的网卡累计值**，以后将增量持久保存到 `/var/lib/vps-toolkit/traffic/machine-meter.json`。跨日/月不归零，重启或网卡计数器重置后保留已记录总量并累加新计数；重启前最后一次采样后的遗漏字节无法恢复，会显示提示。安装前更早的重启历史无法凭空恢复。有可靠的历史总量时，可显式补录**此刻的累计总量**（不是要增加的差值）：

```sh
# 例如实际累计上行 50 GB、累计下行 80 GB：
sudo vps-toolkit traffic ratio-seed --upload-gb 50 --download-gb 80
sudo vps-toolkit traffic status
```

校准会重设整机计量基线，保留一份上次记录；**不清零本工具日/月已用额度**。`auto` 首次选择最低 metric 的默认 IPv4 出口（IPv6-only 时使用 IPv6 出口），此后固定该接口，避免把 lo、docker/veth、桥接和物理网卡重复相加。多出口请自行指定需要合计的公网接口；不要同时选桥接/隧道和其承载网卡：

```sh
ip -br link
sudo vps-toolkit traffic configure --ratio-interface eth0
# 更换已计量的接口时，必须校准当前累计总量，再继续下载
sudo vps-toolkit traffic ratio-seed --upload-gb 50 --download-gb 80
# 确有两个独立公网出口时，可 --ratio-interface eth0,eth1
```

计数来自 Linux [`/proc/net/dev` 接口统计](https://docs.kernel.org/networking/statistics.html)，可能包含内网数据和协议开销，**不等同于云厂商计费账单**。云账单使用其他周期时应以其实际规则核对，工具无法保证账单比例。备份恢复不会回滚本机累计计量文件；迁移到另一台机器应重新校准。

手动下载功能保留，默认也遵守比例缺口；已足够时返回 `ratio_satisfied`。确实需要额外手动实测，可明确指定 `traffic test --mb 10 --ignore-ratio`，仍受日/月/任务预算约束，内容照常自动清理。兼容旧入口的显式 `test` 也保留真实测试语义，会产生额外下行。

## PF / Realm

```sh
sudo vps-toolkit pf diagnose
sudo vps-toolkit pf repair                         # 安装/修复二进制，不重启既有服务
sudo vps-toolkit pf install --archive ./realm-x86_64-unknown-linux-musl.tar.gz
sudo vps-toolkit pf rule list
sudo vps-toolkit pf rule add --listen 0.0.0.0:12345 --remote 目标域名:443 --protocol both
sudo vps-toolkit pf rule edit 0 --remote 新目标域名:443 --protocol tcp --apply
sudo vps-toolkit pf rule delete 0 --apply
sudo vps-toolkit pf service start
sudo vps-toolkit pf service enable
sudo vps-toolkit pf service logs
sudo vps-toolkit pf export ./realm-backup.json
sudo vps-toolkit pf import ./realm-backup.json --apply
```

固定 Realm v2.9.6，默认使用 x86_64/ARM64 官方 musl 包，检查 SHA256、压缩包、ELF 架构、动态加载器和 GLIBC 要求，再原子替换。不会为了 GLIBC 错误升级系统 libc。所有软件资源通过统一下载器验证，支持在 `/etc/vps-toolkit/config.json` 的 `trusted_sources` 设置可信 HTTPS 目录作为备用来源（仍需命中相同官方 SHA256）。

规则默认保存不重启，`--apply` 或安装时 `--restart` 才显式应用；启动失败回滚本次配置/二进制。现有 `/etc/realm/config.json`、扩展字段、服务文件、独立 `pf` 入口均保留；没有旧 `pf` 时创建新快捷入口。`vps-toolkit pf legacy` 可进入旧 PF 菜单。TOML 可只读检查/原样导出，不自动重写；转换为 JSON 后再用规则编辑功能。

本工具提供 TCP/UDP、规则增删改查、服务、导入导出及兼容修复，不重新实现上游旧 PF 的通知/隧道等全部交互功能；复杂旧配置保持原样，可继续用旧入口。

## 客户机 Agent 审计与官方卸载

```sh
sudo vps-toolkit agent audit
sudo vps-toolkit agent report --output ./agent-report.json
sudo vps-toolkit agent remove barad --dry-run
sudo vps-toolkit agent verify
```

分别识别 Aliyun 云助手/安全中心/云监控，Tencent TAT/Barad/Sgagent/主机安全及 cloud-init；对比进程、服务、包、目录、自启动位置，报告权限不足/未确认状态。DMI/系统信息不能确认厂商时，只审计；核实云控制台后可 `cloud select aliyun` 或 `cloud select tencent`，不会猜测实例类型。

单项卸载保留逐组件确认：本地程序必须 root 所有、不可被组/其他用户写入，并提供从可信官方发布物核实的 SHA256。例如：

```sh
sudo vps-toolkit agent remove barad --official-script /可信路径/官方卸载程序.sh --sha256 官方可信摘要 --confirm barad
```

安全组件还需先在云控制台按官方要求完成自保护关闭/卸载授权，再传 `--console-authorized`。遇到验证码使用官方交互流程。没有可信程序或授权时报告未完成；不另行强杀/强删安全客户端或绕过防护。官方卸载程序可停止和移除自己的组件。执行后重新审计，脚本退出成功也不等于组件已消失。

### 一键批量卸载（含 cloud-init，3.3.0）

主菜单 **3 检查客户机 Agent** 的结果页新增 `1 一键卸载全部已检测 Agent（含 cloud-init）`；也可进入 **4 Agent 卸载管理 → 1**。可选 `仅监控 Agent`，或保留原单项官方卸载。

批量模式先显示本次可执行、被阻止和排除的清单、官方依据与影响，输入一次 `remove-all` 确认后逐项处理。对已安装、固定路径的卸载程序，自动检查 root 所有权、文件及上级目录权限，记录 SHA256 并在执行前校验未变；这个摘要用于确认后变更校验，**不把本地文件自动算成厂商签名认证**。缺少可信程序、平台不明/不匹配、自保护未授权的项目会明确报告。新发现的组件不会临时加入已确认批次。

内置 TAT 上游固定提交的卸载程序，已审阅并固定摘要、保留来源与原 LICENSE；不用在目标服务器从 GitHub 在线拉取脚本。Barad/Sgagent/CloudMonitor 使用各自本地官方卸载入口；Sgagent 先于 Barad 处理。云助手使用包管理器依赖检查、官方守护程序参数及官方列出的精确目录清理，最后处理云助手/TAT，避免过早中断管理会话。阿里云安全中心缺少已准备的卸载程序时，需按官方链接取得程序或使用控制台卸载，不执行未核实的在线脚本。

```sh
# 只查看计划；默认范围为全部已检测组件，含 cloud-init
sudo vps-toolkit --human agent remove-all --dry-run
# 仅监控插件，不处理助手、安全客户端或 cloud-init
sudo vps-toolkit --human agent remove-all --scope monitoring
# 保留 cloud-init
sudo vps-toolkit --human agent remove-all --keep-cloud-init
# 查看最近逐项结果；完整文件位于 /var/lib/vps-toolkit/agent-removal/latest.json
sudo vps-toolkit --human agent removal-report
```

**操作前通过独立 SSH/VNC 登录。** 云助手/TAT 卸载会使其会话和远程命令功能失效；菜单会询问连接独立性与网络持久化，命令行用 `--confirm-network-independent` 明确声明已核实。安全客户端先按官方流程完成控制台授权，才使用 `--console-authorized`，官方程序仍可能要求人工验证码。

cloud-init 在批量模式默认尽力卸载：先查持久网络配置、备份网络与 SSH 配置，再检查精确软件包依赖；Debian/Ubuntu 用 dpkg，RPM 系用 rpm，均不使用 `--force`、`--nodeps` 或递归自动移除依赖。Alpine 仅在 APK 模拟能确认只移除 cloud-init 时执行。**保留网络配置（包括 50-cloud-init.yaml）、初始化历史和日志，不重启网络/服务器。** 配置在确认后改变、初始化仍在运行、网络尚未持久化时不继续。依赖拒绝、卸载失败或系统不支持安全移除时，尽力采用 [cloud-init 官方禁用标记](https://docs.cloud-init.io/en/latest/howto/disable_cloud_init.html) 禁用下次启动，并标为“仅禁用，未完全卸载”，不会冒充删除成功。配置与历史目录仍存在不等于程序仍安装。

每项执行后复查；程序退出成功但仍有进程/安装证据时标为“仍有残留/需复核”。一个项目失败会继续其他已确认项目，中断会保存已执行和未执行状态。最近报告与上一份报告固定覆盖保存；不清理系统/业务日志，厂商程序可能删除自己的目录和日志。Realm、下载调度、配额与比例规则不属于卸载范围。

客户机云监控、安全客户端和云助手是不同产品：监控采集指标、助手执行管理命令，安全客户端可能提供文件扫描等能力；检测到客户端不证明它已经上传私人文件。卸载本机 Agent 也不意味着宿主机/控制平面停止采集，不能据此保证“无监控”。

cloud-init 先 `agent remove cloud-init --dry-run` 查看依赖模拟与持久网络文件；必须独立确认网络/SSH 持久化，并同时传 `--confirm-network-independent --confirm cloud-init` 才允许包管理器官方卸载。备份网络文件，保留 `50-cloud-init.yaml`，不重启网络。APK 等未实现安全卸载模拟的系统仅审计 cloud-init。

恢复主动卸载的官方组件时，按审计结果中的 `official_document` / 卸载结果中的 `reinstall` 链接，在对应云控制台选择该实例重新安装官方 Agent，再运行 `agent audit` 核对服务与最近心跳；不要混用其他厂商的安装器。cloud-init 可用本发行版包管理器重新安装，并先人工检查备份网络配置，不自动重建网络或重启 SSH。

官方依据与边界见 [审计记录](docs/AUDIT.md)。客户机 Agent、云平台网络、虚拟化宿主机、物理基础设施、控制平面是不同层；本工具只管理客户机组件，控制台展示与客户机状态可能不同。

## 备份、更新和恢复

```sh
sudo vps-toolkit backup create
sudo vps-toolkit backup restore ./backup.tar.gz                 # 只预览
sudo vps-toolkit backup restore ./backup.tar.gz --apply --confirm restore
sudo vps-toolkit update --check
sudo vps-toolkit update
# 国内断网更新：上传新单文件安装器后执行，或用可信摘要校验：
sudo vps-toolkit update --installer ./install.sh --sha256 通过可信来源核实的摘要
sudo vps-toolkit uninstall --confirm uninstall
```

备份限定工具配置、Realm 与下载计量，拒绝路径穿越/符号链接/摘要不匹配；恢复不自动重启任何服务，恢复旧备份也不能降低已经消耗的流量。安装失败恢复本次自身文件与上一版本指针。`uninstall` 移除统一入口和本工具的新定时服务，**保留原 PF、原定时任务、独立模块运行代码、配置、日志与备份**，避免影响仍在使用的模块。

每日 JSON 和任务状态为小文件，会随历史增长；本版本没有自动删历史统计，以免影响月预算和审计。下载文件不会积累。请按 [恢复说明](docs/RECOVERY.md) 管理备份与历史。

## 平台与验证

需要 Linux、Python 3.8+（ssl/fcntl）、64 位 x86_64 或 ARM64；systemd/OpenRC。安装器支持 apt、dnf/yum、apk，也提供 zypper/pacman 分支。容器未运行 init 时可安装和离线检查，不能启用服务。

[兼容与验收矩阵](docs/COMPATIBILITY.md) 区分发行版容器回归、真实 Realm TCP/UDP 验证与尚未完成的云主机验收。[GitHub Actions](https://github.com/KJ20051223/vps-toolkit/actions) 保存每次提交的实际结果。不得将容器通过解释为 Agent 真机卸载、重启网络或云控制台验收通过。

[架构与模块](docs/ARCHITECTURE.md) · [测试记录与安装器摘要](docs/TESTING.md) · [恢复指南](docs/RECOVERY.md)。

MIT；沿用原 traffic-scheduler 计量、限速、来源健康与调度引擎；对 realm-xwPF 的审查、许可和独立兼容实现见审计文档。
