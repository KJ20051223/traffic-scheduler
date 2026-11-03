import ast
import base64
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import zipfile
from vps_toolkit import __version__

source = Path(__file__).resolve().parent
outputs = source.parent.parent / 'outputs'
repo = outputs / 'vps-toolkit'
repo.mkdir(parents=True, exist_ok=True)
for path in source.rglob('*'):
    if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
        target = repo / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding='utf-8')
        target.write_text(text, encoding='utf-8', newline='\n')
runtime = [p for p in repo.rglob('*') if p.is_file() and (p.relative_to(repo).parts[0] in ('vps_toolkit', 'services')
           or p.relative_to(repo).as_posix() in ('compat/__init__.py', 'compat/traffic_scheduler.py', 'install.py',
                                                'run.py', 'traffic_daemon.py', 'realm-assets.json', 'LICENSE', 'REALM-XWPF-LICENSE'))]
for path in runtime:
    if path.suffix == '.py':
        ast.parse(path.read_text(encoding='utf-8'), feature_version=(3, 8))
manifest = {p.relative_to(repo).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(runtime)}
manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
(repo / 'MANIFEST.json').write_bytes(manifest_bytes)
buffer = io.BytesIO()
with tarfile.open(fileobj=buffer, mode='w') as archive:
    for path in sorted(runtime) + [repo / 'MANIFEST.json']:
        data = path.read_bytes()
        info = tarfile.TarInfo(path.relative_to(repo).as_posix())
        info.size, info.mode, info.mtime = len(data), 0o644, 0
        archive.addfile(info, io.BytesIO(data))
payload = gzip.compress(buffer.getvalue(), mtime=0)
encoded = base64.b64encode(payload).decode()
checksum = hashlib.sha256(payload).hexdigest()
extract = '''cat > "$workdir/payload.b64" <<'VPS_TOOLKIT_PAYLOAD'
{payload}
VPS_TOOLKIT_PAYLOAD
"$python_bin" - "$workdir" <<'VPS_TOOLKIT_EXTRACT'
import base64, hashlib, io, pathlib, sys, tarfile
root = pathlib.Path(sys.argv[1]).resolve()
data = base64.b64decode((root / 'payload.b64').read_bytes().strip(), validate=True)
if hashlib.sha256(data).hexdigest() != '{checksum}':
    raise SystemExit('安装包 SHA256 不匹配，未执行代码')
with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as bundle:
    for member in bundle.getmembers():
        path = pathlib.PurePosixPath(member.name)
        if not member.isfile() or path.is_absolute() or '..' in path.parts:
            raise SystemExit('安装包路径无效')
        target = root / member.name
        target.parent.mkdir(parents=True, exist_ok=True)
        with bundle.extractfile(member) as source:
            target.write_bytes(source.read())
VPS_TOOLKIT_EXTRACT'''.format(payload=encoded, checksum=checksum)
installer = (source / 'bootstrap.sh').read_text(encoding='utf-8').replace('__EMBEDDED_PAYLOAD__', extract)
for path in (repo / 'install.sh', outputs / ('install-vps-toolkit-' + __version__ + '.sh'), outputs / 'install-vps-toolkit.sh'):
    path.write_text(installer, encoding='utf-8', newline='\n')
with zipfile.ZipFile(outputs / ('vps-toolkit-' + __version__ + '.zip'), 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in repo.rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts:
            archive.write(path, 'vps-toolkit/' + path.relative_to(repo).as_posix())
print('Built', repo)
print('Installer SHA256', hashlib.sha256(installer.encode()).hexdigest())
