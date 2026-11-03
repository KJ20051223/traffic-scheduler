import json
from pathlib import Path
from .core import configuration
from .platform import detect


CLOUD_MARKERS = {'aliyun': ['alibaba', 'aliyun'], 'tencent': ['tencent', 'tlinux'],
                 'huawei': ['huawei', 'huaweicloud'], 'baidu': ['baidu'], 'ucloud': ['ucloud'],
                 'jdcloud': ['jdcloud', 'jd cloud']}


def identify_cloud(ctx, override=None):
    evidence = []
    for name in ('sys_vendor', 'product_name', 'bios_vendor'):
        path = ctx.path('/sys/class/dmi/id/' + name)
        if path.is_file():
            evidence.append(('DMI ' + name, path.read_text(errors='replace').strip()[:120]))
    release = detect(ctx)['os']
    evidence.append(('os-release', ' '.join(release.get(k, '') for k in ('ID', 'NAME', 'ID_LIKE'))))
    matches = {provider: [label for label, value in evidence if any(x in value.lower() for x in markers)]
               for provider, markers in CLOUD_MARKERS.items()}
    matches = {k: v for k, v in matches.items() if v}
    if override:
        if override not in list(CLOUD_MARKERS) + ['generic']:
            raise ValueError('不支持的云平台选择')
        return {'provider': override, 'confidence': '用户指定', 'evidence': ['手动选择'],
                'instance_type': '客户机侧无法可靠区分 ECS/轻量或 CVM/Lighthouse，需控制台核实'}
    if len(matches) == 1:
        provider, reasons = next(iter(matches.items()))
        return {'provider': provider, 'confidence': '中' if any('DMI' in x for x in reasons) else '低',
                'evidence': reasons, 'instance_type': '待控制台核实'}
    return {'provider': 'unknown', 'confidence': '不足', 'evidence': ['未发现一致的平台标记'], 'instance_type': '未知'}


def report(ctx):
    results = {'platform': detect(ctx), 'cloud': identify_cloud(ctx, configuration(ctx).get('cloud_override'))}
    commands = {'interfaces': ['ip', '-brief', 'address'], 'routes': ['ip', 'route', 'show', 'default'],
                'listeners': ['ss', '-lntup'], 'ssh': ['ss', '-tnp'], 'virtualization': ['systemd-detect-virt'],
                'memory': ['free', '-m'], 'disk': ['df', '-h', '/']}
    for key, command in commands.items():
        result = ctx.execute(command, timeout=5)
        results[key] = {'ok': result.returncode == 0, 'details': result.stdout[:6000] or result.stderr[:300]}
    dns = ctx.path('/etc/resolv.conf')
    results['dns'] = [line.strip() for line in dns.read_text(errors='replace').splitlines()
                      if line.strip().startswith('nameserver')] if dns.exists() else []
    results['existing'] = {name: ctx.path(path).exists() for name, path in {
        'pf': '/usr/local/bin/pf', 'realm': '/usr/local/bin/realm', 'realm_config': '/etc/realm/config.json',
        'traffic_scheduler': '/usr/local/bin/traffic-scheduler',
        'traffic_config': '/etc/traffic-scheduler/config.json', 'backup': '/var/lib/vps-toolkit/backups'}.items()}
    return results
