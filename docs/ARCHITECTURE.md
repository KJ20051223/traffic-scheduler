# 架构与模块

`vps-toolkit` → `run.py` → `vps_toolkit/cli.py`，命令行与中文菜单调用相同实现。没有 HTTP 控制接口，不监听网站或 TLS 端口。

| 文件 | 职责 |
|---|---|
| bootstrap.sh / install.sh / install.py | 依赖、内嵌载荷验证、独立版本目录、原子 current 指针、失败恢复 |
| core.py | 路径边界、原子配置、受限日志、确认与备份 |
| platform.py / health.py | 包管理器、init、架构/libc、云厂商与只读健康检查 |
| agent.py | 组件分别识别、官方卸载计划、摘要/授权/确认、执行后复查 |
| network.py | 软件下载的 TLS、摘要、大小/超时/重试、可信备用源、离线验证 |
| forwarder.py | 官方固定 Realm、ELF 检查、规则与服务、失败回滚 |
| traffic.py | 新任务授权、手动下载、日/月/任务预算、固定/加权随机调度、暂停恢复 |
| compat/traffic_scheduler.py | 可独立运行的旧引擎，保留来源随机与冷却，挂接统一额度上限 |
| backup.py / maintenance.py | allowlist 备份/恢复、受校验更新、保留模块的管理器卸载 |
| services/ / traffic_daemon.py | systemd 定时器与 OpenRC 等价入口，均默认不启用新任务 |

新旧下载共用 `/var/lib/traffic-scheduler/run.lock` 和 quota 日期记录。先预留再读 HTTP 有效负载，读到的字节始终计入；崩溃后把未结算预留保守计入。恢复备份也持同一锁，不能降低已消耗值。不同下载入口实际串行，Realm 不参与下载锁，也不因下载任务停止而被重启。

手动下载的任务状态只保存 URL、偏移和已计量字节。匿名临时内容关闭后清理，不能用这个工具保存完整文件。下载工作由独立 traffic-scheduler 用户执行；root 菜单通过 runuser 派生子进程，不在进程内降低菜单权限。

首次创建主配置为默认关闭；发现旧配置则继承参数与历史授权，不改旧服务开机/活动状态。安装不重新创建旧定时器。明确执行 `enable --replace-legacy` 才切换调度入口。单个模块仍有入口，不要求卸载现有 PF。

源码测试：`python3 -m unittest -v tests.test_toolkit compat.test_traffic_scheduler`。`build.py` 从源码生成输出目录、MANIFEST、单文件安装器和 ZIP。MANIFEST 覆盖所有运行时代码和服务文件，不包含文档和测试；更新通过完整安装器摘要固定到已发布提交。许可证与兼容旧版本目录一并保留。
