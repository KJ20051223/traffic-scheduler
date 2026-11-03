import json
from pathlib import Path
import ssl
import tempfile
from urllib.request import HTTPSHandler, ProxyHandler, build_opener
from . import __version__
from .core import Error, confirm, digest
from .network import Redirects, fetch
from .platform import detect, service


def update(ctx, args):
    if args.installer:
        ctx.require_root()
        if not args.sha256:
            raise Error('本地更新必须提供通过可信来源核实的 --sha256')
        with tempfile.TemporaryDirectory() as folder:
            verified = fetch(None, Path(folder) / 'install.sh', args.sha256, local=args.installer, kind='shell')
            return {'returncode': ctx.execute(['sh', str(verified), '--no-wizard'], timeout=600, check=True).returncode}
    opener = build_opener(ProxyHandler({}), Redirects(), HTTPSHandler(context=ssl.create_default_context()))
    with opener.open('https://raw.githubusercontent.com/KJ20051223/vps-toolkit/main/update.json', timeout=15) as response:
        descriptor = json.loads(response.read(65536))
    if args.check:
        return {'installed': __version__, 'available': descriptor['version'], 'sha256': descriptor['sha256']}
    ctx.require_root()
    with tempfile.TemporaryDirectory() as folder:
        verified = fetch(descriptor['url'], Path(folder) / 'install.sh', descriptor['sha256'], kind='shell')
        result = ctx.execute(['sh', str(verified), '--no-wizard'], timeout=600, check=True)
        return {'returncode': result.returncode, 'version': descriptor['version']}


def uninstall(ctx, confirmation):
    ctx.require_root()
    confirm('uninstall', confirmation)
    init = detect(ctx)['init']
    names = ['vps-toolkit-traffic.timer', 'vps-toolkit-traffic.service'] if init == 'systemd' else ['vps-toolkit-traffic'] if init == 'openrc' else []
    for name in names:
        service(ctx, name, 'disable')
        service(ctx, name, 'stop')
        file = ctx.path('/etc/systemd/system/' + name if init == 'systemd' else '/etc/init.d/' + name)
        if file.exists() and 'Managed by vps-toolkit' in file.read_text():
            file.unlink()
    command = ctx.path('/usr/local/bin/vps-toolkit')
    if command.exists() and 'VPS_TOOLKIT_MANAGED' in command.read_text()[:300]:
        command.unlink()
    if init == 'systemd':
        ctx.execute(['systemctl', 'daemon-reload'])
    return {'uninstalled': '统一入口与本工具定时服务',
            'preserved': ['PF/Realm 与原定时任务状态', '独立 pf / traffic-scheduler 入口及其运行代码', '配置、计量、日志和备份'],
            'note': '保留模块运行所需的 /opt/vps-toolkit/current，确保两个模块继续独立运行。'}
