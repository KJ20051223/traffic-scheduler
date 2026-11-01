#!/usr/bin/env python3
"""Optional synthetic-data receiver, restricted to explicit client IPv4 addresses."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import socket


class Receiver(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_POST(self):
        if self.client_address[0] not in self.server.allowed:
            self.send_error(403)
            return
        if self.path != '/upload':
            self.send_error(404)
            return
        try:
            length = int(self.headers.get('Content-Length', '-1'))
        except ValueError:
            self.send_error(400)
            return
        if not 0 < length <= 64 * 1024 * 1024 or self.headers.get('Transfer-Encoding'):
            self.send_error(413)
            return
        self.connection.settimeout(30)
        received = 0
        try:
            while received < length:
                chunk = self.rfile.read(min(32768, length - received))
                if not chunk:
                    self.send_error(400)
                    return
                received += len(chunk)
        except (OSError, socket.timeout):
            self.close_connection = True
            return
        data = json.dumps({'received_bytes': received}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--allow-ip', action='append', required=True)
    p.add_argument('--bind', default='0.0.0.0')
    p.add_argument('--port', type=int, default=8765)
    args = p.parse_args()
    allowed = set()
    for address in args.allow_ip:
        ip = ipaddress.ip_address(address)
        if ip.version != 4 or not ip.is_global:
            p.error('--allow-ip 必须是发送方的公网 IPv4')
        allowed.add(str(ip))
    with ThreadingHTTPServer((args.bind, args.port), Receiver) as server:
        server.allowed = allowed
        print('接收端启动；仅接收白名单客户端的合成数据，不保存任何文件', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
