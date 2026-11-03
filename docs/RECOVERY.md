# 状态、失败恢复与独立使用

配置 `/etc/vps-toolkit/config.json`；兼容旧配置 `/etc/traffic-scheduler/config.json`；Realm `/etc/realm/config.json`；计量 `/var/lib/traffic-scheduler/quota-YYYY-MM-DD.json`；任务状态 `/var/lib/vps-toolkit/traffic/`；事件 `/var/log/vps-toolkit/events.jsonl`；备份 `/var/lib/vps-toolkit/backups/`；安装版本 `/opt/vps-toolkit/releases/`。

安装仅更新本工具文件与 current 版本指针，不停原 Realm 和旧下载服务。旧 pf 保持独立，旧 traffic-scheduler 升级为兼容入口；仍可 `sudo traffic-scheduler status`。新 pf 快捷入口没有参数时需 `pf status` 或 `vps-toolkit` 菜单；统一入口卸载不会破坏两模块所需代码。

`pf rule ... --apply` 失败自动恢复原配置并尝试恢复旧服务，仍失败时查看 `pf diagnose` 的日志；系统防火墙和安全组需管理员单独处理。安装包离线传输时，先在可信本地核实完整安装器摘要。下载中断只保留偏移和计量，内容不落为可用文件；可恢复未要求完整摘要的任务，源不支持 Range 时重新开始并继续计量。

备份会包含你授权的 URL 和转发目标，应按敏感运维配置保存，默认权限受限。恢复必须先预览，--apply 显式执行，不重启服务。恢复旧额度记录保留较大的已消耗值，避免重复预算。

每日统计不能在当前月删除，否则无法准确执行月预算。历史归档建议在完整备份后将**上个月以前**的 quota 文件存到独立归档盘，保留当月文件；不要删除当前锁/预留/活动任务文件。任务 JSON 和备份也会增长，本版不自动清理历史，事件日志本身有 2 MB 上限。下载临时内容与这些小型审计文件不同，不会持续占用大文件空间。

仅撤下统一管理工具：`sudo vps-toolkit uninstall --confirm uninstall`。它不卸载 PF/Realm、不关闭旧定时任务、不删除配置/统计/日志。需要完全停下行先执行 `traffic disable`；该操作不作用于 Realm。没有自动清空所有模块的强制卸载选项。
