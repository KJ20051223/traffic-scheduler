# VPS Toolkit

Linux VPS 命令行工具，合并原 traffic-scheduler 的定时下行、手动下载测试、PF/Realm 管理、云平台检测与客户机 Agent 审计。版本 **3.0.0**。

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

卸载仅执行你核实的官方程序，**每个组件单独确认**，本地程序必须 root 所有、不可被组/其他用户写入，并提供从可信官方发布物核实的 SHA256。例如：

```sh
sudo vps-toolkit agent remove barad --official-script /可信路径/官方卸载程序.sh --sha256 官方可信摘要 --confirm barad
```

安全组件还需先在云控制台按官方要求完成自保护关闭/卸载授权，再传 `--console-authorized`。遇到验证码使用官方交互流程。没有可信程序或授权时停止，不杀进程、不改属性、不绕过防护、不强删目录、不抹日志。执行后重新审计，脚本退出成功也不等于组件已消失。

cloud-init 先 `agent remove cloud-init --dry-run` 查看依赖模拟与持久网络文件；必须独立确认网络/SSH 持久化，并同时传 `--confirm-network-independent --confirm cloud-init` 才允许包管理器官方卸载。备份网络文件，保留 `50-cloud-init.yaml`，不重启网络。APK 等未实现安全卸载模拟的系统仅审计 cloud-init。

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

MIT；沿用原 traffic-scheduler 计量、限速、来源健康与调度引擎；对 realm-xwPF 的审查、许可和独立兼容实现见审计文档。
