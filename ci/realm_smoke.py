"""Run the pinned native Realm binary against local TCP and UDP echo servers."""
import json
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vps_toolkit.forwarder import inspect_elf
from vps_toolkit.network import fetch

arch = {'arm64': 'aarch64', 'aarch64': 'aarch64', 'x86_64': 'x86_64'}[platform.machine()]
asset = json.loads(Path('realm-assets.json').read_text())['assets']['realm-' + arch + '-unknown-linux-musl.tar.gz']
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    package = fetch(asset['url'], root / 'realm.tar.gz', asset['sha256'], kind='gzip')
    with tarfile.open(package) as bundle:
        entry = next(m for m in bundle.getmembers() if Path(m.name).name == 'realm' and m.isfile())
        binary = root / 'realm'
        binary.write_bytes(bundle.extractfile(entry).read())
    binary.chmod(0o755)
    print(inspect_elf(binary, arch))
    subprocess.run([str(binary), '--version'], check=True)
    for protocol, socktype in [('tcp', socket.SOCK_STREAM), ('udp', socket.SOCK_DGRAM)]:
        with socket.socket(socket.AF_INET, socktype) as echo, socket.socket() as reservation:
            echo.bind(('127.0.0.1', 0))
            echo.settimeout(6)
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
            reservation.close()
            errors = []
            def respond():
                try:
                    if protocol == 'tcp':
                        echo.listen()
                        connection, _ = echo.accept()
                        with connection:
                            connection.sendall(connection.recv(1024))
                    else:
                        content, address = echo.recvfrom(1024)
                        echo.sendto(content, address)
                except Exception as exc:
                    errors.append(exc)
            worker = threading.Thread(target=respond)
            worker.start()
            config = root / 'config.json'
            config.write_text(json.dumps({'network': {'no_tcp': protocol == 'udp', 'use_udp': protocol == 'udp'},
                'endpoints': [{'listen': '127.0.0.1:' + str(port), 'remote': '127.0.0.1:' + str(echo.getsockname()[1])}]}))
            process = subprocess.Popen([str(binary), '-c', str(config)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                time.sleep(1)
                assert process.poll() is None, process.communicate()[0]
                with socket.socket(socket.AF_INET, socktype) as client:
                    client.settimeout(4)
                    client.connect(('127.0.0.1', port))
                    client.sendall(b'vps-toolkit-native-test')
                    assert client.recv(1024) == b'vps-toolkit-native-test'
            finally:
                process.terminate()
                print(process.communicate(timeout=5)[0].decode(errors='replace'))
                worker.join(7)
            assert not errors, errors
            print(arch, protocol, 'PASS')
