"""Small shared helpers for local and GitHub Actions builds (Python 3.12+)."""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def public_url(url: str) -> str:
    """Do not retain signed query strings or URL credentials in build artifacts."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ''
    if ':' in host:
        host = '[' + host + ']'
    if parsed.port is not None:
        host += ':' + str(parsed.port)
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, '', ''))


def download(url: str, output: Path) -> dict:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username:
        raise ValueError('Download links must be HTTP(S) URLs without embedded credentials')
    output.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'galaxy-watch-bakasu-boot-build'})
            with urllib.request.urlopen(request, timeout=120) as response, output.open('wb') as stream:
                if urllib.parse.urlsplit(response.url).scheme not in ('http', 'https'):
                    raise ValueError('Download redirected to a non-HTTP(S) URL')
                shutil.copyfileobj(response, stream, length=8 << 20)
            if not output.stat().st_size:
                raise ValueError('Downloaded file is empty')
            return {'url': public_url(url), 'sha256': sha256(output), 'bytes': output.stat().st_size}
        except (OSError, urllib.error.URLError):
            if attempt == 2:
                raise RuntimeError(f'Download failed after three attempts: {public_url(url)}') from None
            time.sleep(2 * (attempt + 1))
    raise AssertionError('unreachable')
