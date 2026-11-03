import hashlib
import hmac
import os
from pathlib import Path
import re
import ssl
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
from .core import Error, digest


class Redirects(HTTPRedirectHandler):
    max_redirections = 5
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != 'https':
            raise Error('代码下载不接受非 HTTPS 重定向')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, destination, sha256, mirrors=(), local=None, kind=None, limit=100 * 1024 * 1024, timeout=30):
    if not re.fullmatch(r'[0-9a-fA-F]{64}', sha256 or ''):
        raise Error('下载必须提供可信 SHA256')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    candidates = [local] if local else [url] + list(mirrors)
    errors = []
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), Redirects())
    for source in candidates:
        for attempt in range(1 if local else 3):
            tmp = None
            try:
                if not local:
                    parsed = urlsplit(source)
                    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
                        raise Error('代码来源必须是无凭据的 HTTPS URL')
                fd, tmp = tempfile.mkstemp(prefix='.download-', dir=str(destination.parent))
                request = Path(source).open('rb') if local else opener.open(Request(source, headers={'User-Agent': 'vps-toolkit'}), timeout=timeout)
                with os.fdopen(fd, 'wb') as output, request as incoming:
                    size = 0
                    started = time.monotonic()
                    while True:
                        block = incoming.read(65536)
                        if not block:
                            break
                        size += len(block)
                        if size > limit or time.monotonic() - started > timeout:
                            raise Error('下载超出大小或运行时间限制')
                        output.write(block)
                if not hmac.compare_digest(digest(tmp), sha256.lower()):
                    raise Error('SHA256 不匹配')
                with open(tmp, 'rb') as handle:
                    head = handle.read(512).lstrip()
                if head.lower().startswith((b'<!doctype html', b'<html')):
                    raise Error('下载结果是错误网页')
                if kind == 'shell' and not head.startswith(b'#!'):
                    raise Error('下载结果不是带 shebang 的脚本')
                if kind == 'gzip' and not head.startswith(b'\x1f\x8b'):
                    raise Error('下载结果不是 gzip 压缩包')
                os.replace(tmp, str(destination))
                return destination
            except (OSError, ValueError, Error) as exc:
                errors.append('{}: {}'.format(source if not local else '本地文件', exc))
                if attempt < 2 and not local:
                    time.sleep(attempt + 1)
            finally:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
    raise Error('所有可信来源均失败: ' + '; '.join(errors[-3:]))
