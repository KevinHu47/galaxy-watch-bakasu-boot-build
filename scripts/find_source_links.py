#!/usr/bin/env python3
"""Resolve a Samsung model/build to ordered Internet Archive source URLs.

Samsung's public search table supplies upload IDs and supported builds. Archive
metadata supplies actual filenames; small overlay ZIP READMEs supply base-source
dependencies. No source files or instructions from the archives are executed.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from common import write_json


SAMSUNG_SEARCH = 'https://opensource.samsung.com/uploadSearch'
ARCHIVE = 'https://archive.org'
MAX_OVERLAY_SIZE = 16 << 20
MODEL_RE = re.compile(r'SM-[A-Z0-9]{3,16}')


def firmware_in_name(firmware: str, name: str) -> bool:
    return firmware in re.split(r'[^A-Z0-9]+', name.upper())


def validate_inputs(model: str, firmware: str) -> tuple[str, str]:
    model, firmware = model.strip().upper(), firmware.strip().upper()
    if not MODEL_RE.fullmatch(model):
        raise ValueError('型号格式无效，请填写例如 SM-L315F。')
    if not re.fullmatch(r'[A-Z0-9]{8,40}', firmware) or not firmware.startswith(model[3:]):
        raise ValueError('固件版本必须是该型号的完整构建号，例如 L315FXXS3BZF4。')
    return model, firmware


def normalize_item(value: str) -> str:
    value = value.strip()
    if value.startswith('https://'):
        parsed = urllib.parse.urlsplit(value)
        parts = parsed.path.strip('/').split('/')
        if (parsed.netloc != 'archive.org' or len(parts) < 2
                or parts[0] not in ('details', 'download', 'metadata')):
            raise ValueError('archive_item 必须是 Archive.org 的三星开源归档编号或详情链接。')
        value = parts[1]
    if value.isdigit():
        value = 'samsung-opensource-' + value
    if not re.fullmatch(r'samsung-opensource-\d+', value):
        raise ValueError('archive_item 格式无效，例如 samsung-opensource-14120 或 14120。')
    return value


class SamsungSearchParser(HTMLParser):
    """Read desktop table rows; ignore the duplicate mobile download rows."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.cells = None
        self.cell = None
        self.upload_id = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'tr':
            self.cells, self.cell, self.upload_id = {}, None, None
        elif tag == 'td' and self.cells is not None:
            classes = attrs.get('class', '').split()
            self.cell = next((c for c in classes if c in ('col-model', 'col-version', 'col-desc')), None)
            if self.cell:
                self.cells[self.cell] = [attrs.get('title', '')]
        elif tag == 'br' and self.cell:
            self.cells[self.cell].append('\n')
        elif tag == 'a' and self.cells is not None:
            match = re.search(r'''showSrcDownPop\(['"](\d+)['"]''', attrs.get('href', ''))
            if match:
                self.upload_id = match.group(1)

    def handle_data(self, data):
        if self.cell:
            self.cells[self.cell].append(data)

    def handle_endtag(self, tag):
        if tag == 'td':
            self.cell = None
        elif tag == 'tr' and self.cells is not None:
            if self.upload_id and 'col-model' in self.cells:
                self.rows.append({
                    'upload_id': self.upload_id,
                    'models': sorted(set(re.findall(r'\bSM-[A-Z0-9]+\b', ''.join(self.cells['col-model']).upper()))),
                    'versions': sorted(set(re.findall(r'\b[A-Z0-9]{8,40}\b', ''.join(self.cells.get('col-version', [])).upper()))),
                    'filenames': sorted(set(re.findall(r'[A-Za-z0-9_.+-]+\.zip', ''.join(self.cells.get('col-desc', [])), re.I))),
                })
            self.cells, self.cell = None, None


class Client:
    def read(self, url: str, limit: int = 8 << 20) -> bytes:
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers={
                    'User-Agent': 'galaxy-watch-bakasu-boot-build source-link-resolver',
                })
                with urllib.request.urlopen(request, timeout=45) as response:
                    data = response.read(limit + 1)
                if len(data) > limit:
                    raise ValueError(f'响应超过 {limit} 字节，停止解析：{url}')
                return data
            except (OSError, urllib.error.URLError) as error:
                if isinstance(error, urllib.error.HTTPError) and error.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f'请求失败 HTTP {error.code}：{url}') from error
                if attempt == 2:
                    raise RuntimeError(f'请求三次失败：{url} ({error})') from error
                time.sleep(2 * (attempt + 1))
        raise AssertionError('unreachable')

    def metadata(self, item: str) -> dict:
        data = json.loads(self.read(f'{ARCHIVE}/metadata/{item}'))
        if data.get('metadata', {}).get('identifier') != item or not data.get('files'):
            raise ValueError(f'Archive.org 未收录该归档或没有文件：{item}')
        return data


def discover_rows(client: Client, model: str, firmware: str) -> tuple[list[dict], list[str]]:
    errors = []
    # Firmware search also finds builds listed only inside update filenames.
    for query in (firmware, model):
        url = SAMSUNG_SEARCH + '?' + urllib.parse.urlencode({'searchValue': query})
        try:
            body = client.read(url).decode('utf-8', 'replace')
            if 'cf-chl-' in body or 'Just a moment' in body:
                raise ValueError('三星搜索页返回 Cloudflare 验证页面。')
            parser = SamsungSearchParser()
            parser.feed(body)
            matches = [row | {'search_url': url} for row in parser.rows
                       if model in row['models'] and (firmware in row['versions']
                       or any(firmware_in_name(firmware, name) for name in row['filenames']))]
            if matches:
                return list({row['upload_id']: row for row in matches}.values()), errors
        except (ValueError, RuntimeError) as error:
            errors.append(str(error))
    detail = '；'.join(errors) if errors else '三星搜索表中没有型号与完整固件版本同时匹配的记录。'
    raise ValueError(detail + ' 可在 archive_item 中填写已知的 Archive.org 条目编号后重试。')


def model_in_package(model: str, name: str) -> bool:
    # E.g. SM-L315U_L3150_Opensource_dts.zip serves both named models.
    prefix = re.split('_opensource', name, flags=re.I)[0].upper()
    return any(part in (model, model[3:]) for part in prefix.split('_'))


def original_zip_files(metadata: dict) -> list[dict]:
    return [f for f in metadata['files'] if f.get('source') == 'original'
            and f['name'].lower().endswith('.zip')
            and str(f.get('private', '')).lower() not in ('true', '1')]


def file_url(item: str, name: str) -> str:
    return f'{ARCHIVE}/download/{item}/' + urllib.parse.quote(name, safe='')


def read_dependencies(client: Client, item: str, entry: dict) -> dict:
    """Download only small overlay ZIPs; read text, never extract or execute."""
    size = int(entry.get('size', 0))
    if size > MAX_OVERLAY_SIZE:
        return {'readmes': [], 'base_versions': [], 'note': '文件超过 16 MiB，仅核对索引与文件名，未读取 README。'}
    data = client.read(file_url(item, entry['name']), MAX_OVERLAY_SIZE)
    if size and len(data) != size:
        raise ValueError('下载大小与 Archive 元数据不符：' + entry['name'])
    for algorithm in ('sha1', 'md5'):
        if entry.get(algorithm) and hashlib.new(algorithm, data).hexdigest() != entry[algorithm]:
            raise ValueError('下载校验值与 Archive 元数据不符：' + entry['name'])
    readmes, versions = [], set()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            name = member.filename
            if (member.is_dir() or member.file_size > 256 << 10
                    or not re.search(r'(?:readme[^/]*\.txt|[^/]+_kernel\.txt)$', name, re.I)):
                continue
            if len(readmes) >= 16:
                raise ValueError('README 数量异常：' + entry['name'])
            text = archive.read(member).decode('utf-8-sig', 'replace')
            refs = re.findall(r'download\s+and\s+unzip\s+the\s+kernel\s+source\s+of\s+([A-Z0-9]{8,40})', text, re.I)
            versions.update(ref.upper() for ref in refs)
            readmes.append({'name': name, 'base_versions': sorted(set(ref.upper() for ref in refs))})
    return {'readmes': readmes, 'base_versions': sorted(versions)}


def select_packages(model: str, firmware: str, metadata: dict, row: dict | None,
                    read_overlay) -> dict:
    """Select only an unambiguous package set within one verified archive item."""
    files = original_zip_files(metadata)
    names = [f['name'] for f in files]
    if len(set(names)) != len(names):
        raise ValueError('归档含重复文件名，无法确定下载对象。')
    exact = [f for f in files if firmware_in_name(firmware, f['name'])]
    if not exact and (row is None or firmware not in row['versions']):
        raise ValueError('归档文件名及三星版本列表均没有精确匹配完整固件版本。')
    bases = [f for f in files if re.fullmatch(r'SM-[A-Z0-9]+(?:_[A-Z0-9]+)*_Opensource\.zip', f['name'], re.I)]
    devices = [f for f in files if f['name'].lower().endswith('_dts.zip')
               and model_in_package(model, f['name'])]
    # Prefer a build-specific DTS only when it names this exact build.
    exact_devices = [f for f in devices if firmware_in_name(firmware, f['name'])]
    devices = exact_devices or [f for f in devices if not re.search(
        r'_[A-Z0-9]{8,40}(?:_|\.zip)', re.split('_opensource', f['name'], flags=re.I)[-1], re.I)]
    updates = [f for f in exact if f not in devices and not f['name'].lower().endswith('_dts.zip')]
    if len(devices) > 1 or len(updates) > 1:
        raise ValueError('型号适配包或固件更新包有多个候选，无法自动确定合并顺序。')
    if not bases:
        raise ValueError('该归档没有可识别的完整基础包（*_Opensource.zip），不能把增量包当成完整源码。')
    overlays = [(f, 'device') for f in devices] + [(f, 'update') for f in updates]
    evidence = {f['name']: read_overlay(f) for f, _ in overlays}
    dependencies = sorted({version for e in evidence.values() for version in e['base_versions']})
    if len(dependencies) > 1:
        raise ValueError('适配包和更新包要求不同的基础版本，需人工检查：' + ', '.join(dependencies))
    if dependencies:
        bases = [f for f in bases if dependencies[0].startswith(f['name'].split('_', 1)[0][3:])]
        if row and dependencies[0] not in row['versions']:
            raise ValueError('README 要求的基础版本不在该三星条目的版本列表中：' + dependencies[0])
    elif len(bases) > 1:
        bases = [f for f in bases if model_in_package(model, f['name'])]
    if len(bases) != 1:
        raise ValueError('无法唯一确定 README 所需的完整基础包。')
    if not devices and not model_in_package(model, bases[0]['name']):
        raise ValueError('基础包属于其他型号，归档缺少目标型号的明确适配包。')
    if any(f not in [entry for entry, _ in overlays] for f in exact):
        raise ValueError('有完整固件版本匹配的文件无法归类，需人工确认依赖。')
    item = metadata['metadata']['identifier']
    packages = []
    for f, role in [(bases[0], 'base'), *overlays]:
        packages.append({'role': role, 'name': f['name'], 'url': file_url(item, f['name']),
                         'bytes': int(f.get('size', 0)), 'sha1': f.get('sha1'),
                         'dependency_evidence': evidence.get(f['name'])})
    notes = ['链接与文件名来自 Archive.org 实际文件清单；顺序为基础包、型号适配包、精确版本更新包。',
             '只分析源码链接及 README 依赖；完整源码内容、可编译性与实机兼容性尚未由本次查找验证。']
    if not row:
        notes.append('使用指定归档，未查询三星版本表；固件版本匹配依据为归档中的完整文件名。')
    if not dependencies:
        notes.append('未识别到 README 基础版本依赖；包顺序依据文件名分类，请核对源包说明。')
    return {'archive_item': item, 'archive_url': f'{ARCHIVE}/details/{item}',
            'metadata_url': f'{ARCHIVE}/metadata/{item}', 'base_versions': dependencies,
            'packages': packages, 'source_urls': ';'.join(p['url'] for p in packages), 'notes': notes}


def resolve(client: Client, model: str, firmware: str, item: str, report: dict) -> dict:
    if item:
        rows = [None]
        items = [normalize_item(item)]
        report['discovery'] = {'method': 'explicit_archive_item'}
    else:
        rows, errors = discover_rows(client, model, firmware)
        items = ['samsung-opensource-' + row['upload_id'] for row in rows]
        report['discovery'] = {'method': 'samsung_search', 'rows': rows, 'request_errors': errors}
    candidates, errors = [], []
    for item_id, row in zip(items, rows):
        try:
            metadata = client.metadata(item_id)
            result = select_packages(model, firmware, metadata, row,
                                     lambda f: read_dependencies(client, item_id, f))
            candidates.append(result)
        except (ValueError, RuntimeError, zipfile.BadZipFile) as error:
            errors.append({'archive_item': item_id, 'error': str(error)})
    report['candidate_errors'] = errors
    # Do not hide an unreadable candidate that could contain another match.
    if len(candidates) != 1 or errors:
        report['candidates'] = candidates
        raise ValueError('未能唯一解析全部候选归档。请查看候选与错误，必要时指定 archive_item。')
    return candidates[0]


def markdown(value) -> str:
    return html.escape(str(value)).replace('|', '&#124;').replace('`', '&#96;').replace('\r', '').replace('\n', ' ')


def summary(report: dict) -> str:
    lines = ['# 三星开源源码链接分析', '',
             f"型号：**{markdown(report['model'])}**　固件版本：**{markdown(report['firmware'])}**", '']
    if report['status'] != 'success':
        lines += ['**解析失败**：' + markdown(report['error']), '',
                  '未生成可直接用于编译的 source_urls。']
        for candidate in report.get('candidates', []):
            lines += ['', f"候选归档：[{candidate['archive_item']}]({candidate['archive_url']})"]
        for error in report.get('candidate_errors', []):
            lines += ['', f"- {markdown(error['archive_item'])}：{markdown(error['error'])}"]
    else:
        result = report['result']
        lines += [f"归档：[{result['archive_item']}]({result['archive_url']})", '',
                  '按下表顺序填入编译 Action 的 `source_urls`：', '',
                  '| 顺序 | 用途 | 下载直链 | 大小 |', '| --- | --- | --- | --- |']
        roles = {'base': '完整基础包', 'device': '型号适配包', 'update': '固件版本更新包'}
        for i, package in enumerate(result['packages'], 1):
            lines.append(f"| {i} | {roles[package['role']]} | [{markdown(package['name'])}]({package['url']}) | {package['bytes']:,} B |")
        lines += ['', '## 可直接复制的 source_urls', '', '```text', result['source_urls'], '```', '']
        if result['base_versions']:
            lines += ['README 要求的基础源码版本：' + ', '.join(f'`{v}`' for v in result['base_versions']) + '。', '']
        discovery = report['discovery']
        for row in discovery.get('rows', []):
            lines += [f"三星索引：[型号／版本查询]({row['search_url']})，upload ID `{row['upload_id']}`。", '']
        lines += ['- ' + markdown(note) for note in result['notes']]
    return '\n'.join(lines) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default=os.environ.get('MODEL', ''))
    parser.add_argument('--firmware', default=os.environ.get('FIRMWARE', ''))
    parser.add_argument('--archive-item', default=os.environ.get('ARCHIVE_ITEM', ''))
    parser.add_argument('--output', type=Path, default=Path('dist/source-links'))
    args = parser.parse_args()
    report = {'model': args.model.strip().upper(), 'firmware': args.firmware.strip().upper(), 'status': 'failed'}
    try:
        model, firmware = validate_inputs(args.model, args.firmware)
        report['result'] = resolve(Client(), model, firmware, args.archive_item, report)
        report['status'] = 'success'
    except (ValueError, RuntimeError, OSError, zipfile.BadZipFile) as error:
        report['error'] = str(error)
    args.output.mkdir(parents=True, exist_ok=True)
    # A failed rerun must not leave a previous successful set of links behind.
    url_file = args.output / 'source-urls.txt'
    url_file.unlink(missing_ok=True)
    write_json(args.output / 'source-links.json', report)
    rendered = summary(report)
    (args.output / 'summary.md').write_text(rendered, encoding='utf-8')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as stream:
            stream.write(rendered)
    if report['status'] == 'success':
        result = report['result']
        url_file.write_text(result['source_urls'] + '\n', encoding='utf-8')
        if os.environ.get('GITHUB_OUTPUT'):
            with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as stream:
                stream.write(f"source_urls={result['source_urls']}\narchive_item={result['archive_item']}\n")
        print(result['source_urls'])
        return 0
    print('源码链接解析失败：' + report['error'], file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
