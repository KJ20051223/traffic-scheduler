# 3.0.0 验收记录

运行时代码提交：33178c99c7e9ebb711d5c23d67013fe090961718。

单文件安装器 SHA256：`57a8268c67a4f01d0e1287f18293020ac9469908d1f5f5680c4c268d6b8390e4`。update.json 固定到该提交的安装器，文档修改不改变此载荷。

本地 Windows / Python 3.13：88 项测试通过，6 项 POSIX 专属检查跳过。包括本地真实 HTTP Range 下载、连续偏移、SHA256、失败实际字节计量、自动内容清理、日/月/任务预算、暂停与恢复、午夜截止、估算权重/时间窗口/补排、旧入口共享预算与时区、备份保留已用与未结算预留、组件隔离、确认取消与官方卸载返回错误、配置回滚。POSIX 文件锁和安装事务在 Linux CI 验证。

Shell 安装器与 OpenRC 服务语法已检查；运行时文件用 Python 3.8 语法解析，并按 MANIFEST.json 核对所有文件摘要。

Linux 全量结果和对应工作流见 [Actions](https://github.com/KJ20051223/vps-toolkit/actions)。每个发行版任务实际运行单文件安装、读取默认关闭的配置、运行同一套回归测试，再进行离线重装。Realm 在原生 x86_64 / ARM64 runner 上验证官方 SHA256、ELF、版本并真实转发 TCP/UDP 回环数据。

未验收项目：完整云主机开机/重启、systemd/OpenRC 真正开机自启、真实云安全客户端自保护/验证码卸载、官方控制台状态同步、云网络持久性、全天长期公网文件下载。测试不卸载正在使用的真实安全产品，不修改 SSH、防火墙或安全组。

Debian 11 默认源的 bullseye-security 包实测 404，测试容器单独使用官方归档源；正式安装器不自动改源。AlmaLinux 8/9、Rocky 10、TencentOS 3、openEuler、Anolis、SUSE、Arch 的本轮验收边界见兼容矩阵，不按其他系统的通过结果推定这些系统已经验收。
