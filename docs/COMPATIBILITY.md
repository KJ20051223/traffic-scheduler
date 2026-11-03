# 平台验证范围

安装器最低 Python 3.8，不升级系统 libc；老 RPM 默认 Python 3.6 时从发行版仓库安装并行的新 Python。每个平台是否通过以对应提交的 Actions 结果为准，不能只凭包管理器识别声称全面支持。

| 系统 | 安装分支 / init | 自动化范围 |
|---|---|---|
| Ubuntu 20.04 / 22.04 / 24.04 | apt / systemd | CI 容器真实安装、离线重装、完整回归 |
| Debian 11 / 12 / 13 | apt / systemd | 同上；11 已结束标准 LTS，需自行确认维护源 |
| Alpine 3.20 / 3.23 | apk / OpenRC | 同上；OpenRC 服务路由另有隔离测试 |
| Rocky 8 / 9、AlmaLinux 10 | dnf / systemd | 同上，包含并行 Python 安装路径 |
| CentOS Stream 9 / 10、Fedora 44 | dnf / systemd | 同上 |
| TencentOS Server 4 | dnf / systemd | 官方 tencentos/tencentos4-minimal 容器；云主机 Agent 卸载未验收 |
| Alibaba Cloud Linux 3 | dnf/yum / systemd | 官方 alinux3:230602.1 容器；云主机 Agent 卸载未验收 |
| AlmaLinux 8/9、Rocky 10、TencentOS 3、openEuler、Anolis | dnf/yum / systemd | 已适配识别和安装逻辑，本轮未逐一实机验收 |
| openSUSE / Arch | zypper/pacman | 提供安装分支，未实机验收 |

TencentOS 镜像来源：[官方基础镜像文档](https://intl.cloud.tencent.com/document/product/1330/84362)。Alibaba Linux 镜像来源：[官方容器文档](https://help.aliyun.com/en/alinux/getting-started/use-an-alibaba-cloud-linux-image-in-docker)。若公共仓库/软件源不可达，CI 应报告失败，不能当作通过。

x86_64 与 ARM64 在各自原生 Ubuntu runner 上下载、验证、执行固定 musl Realm，分别测试真实 TCP/UDP 回环转发。单元测试覆盖 HTTP Range 顺序下载及 SHA256、日/月/单任务预算、异常预留、锁冲突、暂停、坏摘要、安装保留状态、规则失败回滚、恢复不重置额度、安全卸载取消等。

没有用容器证明 systemd 开机自启、整机重启网络持久性、厂商自保护/验证码卸载、云控制台显示同步。也未在缺失加载器的实际故障主机升级 libc；实现会预检后拒绝不兼容二进制。
