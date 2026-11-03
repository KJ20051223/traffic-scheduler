# 来源审计与改造边界

## 基线

- 原项目：KJ20051223/traffic-scheduler，提交 aa1d4f1ef827ce4572c997624890c07fbbc4b81b（2.3.0）。复用计量预留/恢复、限速、IPv4 与地区校验、网络重试和随机权重算法；兼容模块保留 51 个回归测试。
- [realm-xwPF](https://github.com/zywe03/realm-xwPF)，审阅提交 ef90071cca0073a4dd41f62708a6c9e82a27873c，MIT，原许可存于 REALM-XWPF-LICENSE。审阅入口 xwPF.sh 与 lib/core.sh、lib/realm.sh 等下载链。
- [Realm](https://github.com/zhboner/realm/releases/tag/v2.9.6)，固定 v2.9.6；realm-assets.json 的 SHA256 来自该官方发布资产 digest。配置依据该版本 src/conf/endpoint.rs，支持每条规则 network.no_tcp/use_udp。

## 修正的问题

上游 PF 引导脚本从 main 下载多个 Shell 文件，未按可信固定摘要验证；二进制下载和更新分散，存在直接覆盖 live 文件、配置变量 eval 等维护风险。上游已优先尝试 musl，但仍缺少统一真实性校验和失败事务回滚。旧 traffic-scheduler 安装/配置流程会停止服务，与这次“升级保留状态”的要求不符。

新实现不在线执行上游 Shell，也不复制其完整菜单：统一受校验下载器（TLS 校验、拒绝 HTTP 重定向、超时/重试/体积限制、HTML 拒绝、SHA256、离线、临时文件），固定官方 Realm 包，验证 ELF/加载器/GLIBC，暂存后原子替换；规则与二进制分别备份、失败回滚。旧 PF 文件、配置扩展和服务保持兼容，复杂旧功能仍通过独立入口使用。

## 官方 Agent 操作依据

| 组件 | 官方依据 | 执行要求 |
|---|---|---|
| 阿里云云助手 | [停止卸载文档](https://help.aliyun.com/zh/ecs/user-guide/stop-and-uninstall-the-cloud-assistant-agent) | 包依赖检查、官方守护程序参数与精确组件目录清理；先确认独立 SSH/VNC |
| 阿里云安全中心 | [官方卸载流程](https://www.alibabacloud.com/help/zh/security-center/uninstall-the-security-center-agent) | 控制台自保护/卸载授权；官方程序与可信 SHA256 |
| 阿里云云监控 | [C++ Agent 安装卸载](https://help.aliyun.com/en/cms/cloudmonitor-1-0/user-guide/install-and-uninstall-the-cloudmonitor-agent-for-cpp) | 已安装 cloudmonitorCtl.sh uninstall，或 legacy wrapper remove，仍需摘要核实 |
| 腾讯 Barad / Sgagent | [监控 Agent](https://intl.cloud.tencent.com/zh/document/product/248/39810) | 两个组件独立，执行各自 admin/uninstall.sh |
| 腾讯 TAT | [官方 TAT 卸载脚本](https://github.com/Tencent/tat-agent/blob/main/install/uninstall.sh) | 可信固定版本脚本+摘要，不直接执行 main 在线脚本 |
| 腾讯主机安全 | [卸载流程](https://cloud.tencent.com/document/product/664/137804) | 按控制台授权/验证码流程，禁止防护绕过 |
| cloud-init | [官方禁用说明](https://cloudinit.readthedocs.io/en/latest/howto/disable_cloud_init.html) | 本工具默认审计；移除须额外核实发行版依赖与网络持久化 |

3.3.0 批量卸载按用户选择默认包含 cloud-init，单项模式保持原有要求。批量仅接受已检测的确切组件，先展示计划并一次确认，不扩展到确认后新出现的组件。已安装程序的 root 所有权、不可替换的父目录、计划 SHA256 和执行前复核共同限制本地卸载入口；自动计算的哈希只防确认后改动，不构成厂商签名认证。

内置 TAT 原始卸载脚本来自官方仓库 [固定提交 3c448fe4](https://github.com/Tencent/tat-agent/blob/3c448fe41fa502d0d3294181d37018380235c6be/install/uninstall.sh)，载荷 `vps_toolkit/assets/tat-uninstall.sh`，SHA256 `f63d2b8fc0c79aa5c612c30bc03360c2c3a7b2f4d205eb7548acb3182a9bae58`；同提交 LICENSE 存于 `vps_toolkit/assets/TAT-LICENSE`，安装器 MANIFEST 同时核对这两个文件。脚本仅移除 TAT 的服务、PID、链接和安装目录；只有管理员确认批次后才执行。开发/CI 不执行该真实脚本卸载本机组件。

追加依据：[腾讯主机安全官方卸载路径](https://intl.cloud.tencent.com/ind/document/product/296/12236)、[腾讯安全客户端自保护流程](https://cloud.tencent.com/document/product/664/137804)、[阿里云安全中心卸载要求](https://help.aliyun.com/zh/security-center/uninstall-the-security-center-agent)。验证码和自保护均保留官方流程；缺少验证程序的阿里安全组件报告未完成。cloud-init 失败备选为官方 `cloud-init.disabled` 标记，仅禁用下次启动，不宣称完成卸载。

只读探测不访问元数据凭证。报告不导出进程命令行、云密钥、token 或用户数据。未检测到已知进程不能推出没有其他监控，也不表示控制平面停止采集。

没有在用户云服务器上实际卸载 Agent、改防火墙、停 SSH 或重启网络。此类真机验收必须由持有控制台权限的管理员在维护窗口执行；测试覆盖的是组件隔离、拒绝未知厂商/未授权操作、确认取消、官方程序失败与重新审计。
