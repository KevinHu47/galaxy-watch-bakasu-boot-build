#!/usr/bin/env python3
"""Extract and merge Galaxy Watch 7 Samsung kernel source archives."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from archive_utils import archive_kind, merge_tree, safe_extract
from common import download, sha256, write_json


def kernel_roots(staging: Path) -> list[tuple[Path, str]]:
    candidates = []
    for directory, subdirs, _ in os.walk(staging, followlinks=False):
        path = Path(directory)
        complete = ((path / 'Makefile').is_file() and (path / 'Kconfig').is_file()
                    and (path / 'arch').is_dir())
        partial = (path.name.lower() == 'kernel' and (
            any((path / part).is_dir() for part in ('arch', 'drivers', 'include', 'fs', 'mm', 'net',
                                                    'security', 'sound', 'block', 'lib', 'scripts', 'certs'))
            or any((path / name).is_file() for name in ('Makefile', 'Kconfig', 'build.config.constants'))))
        if complete or partial:
            candidates.append((path, 'complete' if complete else 'overlay'))
            subdirs[:] = []
    return candidates


def packages(path: Path, staging: Path, origin: str, depth: int = 0):
    if depth > 8:
        raise ValueError('More than eight nested archive layers; check the supplied source package')
    files = safe_extract(path, staging)
    roots = kernel_roots(staging)
    if len(roots) > 1:
        raise ValueError('Archive contains multiple kernel roots; supply their archives separately: ' +
                         ', '.join(str(p.relative_to(staging)) for p, _ in roots))
    if roots:
        root, kind = roots[0]
        yield root, kind, {'member': origin, 'kernel_root': root.relative_to(staging).as_posix()}
        return
    found = False
    for index, item in enumerate(files):
        if item.is_file() and not item.is_symlink() and archive_kind(item):
            found = True
            yield from packages(item, staging.parent / f'{staging.name}-nested-{index}',
                                origin + '!' + item.relative_to(staging).as_posix(), depth + 1)
    if not found:
        raise ValueError(f'No kernel source tree or nested source archive found in {origin}')


def merge_package(root: Path, output: Path, *, include_siblings: bool = True) -> dict:
    summary = {'kernel': merge_tree(root, output, output.parent), 'siblings': {}}
    if not include_siblings:
        return summary
    for item in root.parent.iterdir():
        if item == root or item.name.startswith('.') or item.is_symlink():
            continue
        if item.is_dir():
            summary['siblings'][item.name] = merge_tree(item, output.parent / item.name, output.parent)
        elif item.is_file() and not archive_kind(item):
            target = output.parent / item.name
            if target.exists() and target.is_dir():
                raise ValueError(f'Package file conflicts with directory: {target}')
            if target.is_symlink():
                target.unlink()
            if target.exists():
                target.chmod(target.stat().st_mode | 0o600)
            shutil.copy2(item, target)
    return summary


def merge_archives(paths: list[Path], output: Path, manifest: Path, identities: list[dict] | None = None) -> dict:
    if output.exists():
        raise ValueError('Source output already exists; use a new work directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'archives': [], 'packages': []}
    with tempfile.TemporaryDirectory(prefix='samsung-source-', dir=output.parent.parent) as temp:
        for index, path in enumerate(paths):
            identity = identities[index] if identities else {'file': path.name, 'sha256': sha256(path)}
            report['archives'].append(identity)
            print(f'Extracting GW7 source archive {index + 1}/{len(paths)}', flush=True)
            for root, kind, record in packages(path, Path(temp) / f'archive-{index}', path.name):
                if not report['packages'] and kind != 'complete':
                    raise ValueError('The first source package must contain a full kernel tree; put DTS/update packages later')
                record.update({'input_index': index + 1, 'kind': kind,
                               'merge': merge_package(root, output, include_siblings=record['kernel_root'] != '.')})
                report['packages'].append(record)
                print(f'  {kind}: {record["member"]} -> src/kernel', flush=True)
    if not (output / 'Makefile').is_file() or not (output / 'arch').is_dir():
        raise ValueError('No complete kernel source tree was produced')
    write_json(manifest, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--urls', help='Semicolon-separated links (or SOURCE_URLS environment variable)')
    parser.add_argument('--archive', type=Path, action='append', help='Local archive, for local builds/tests')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--downloads', type=Path, required=True)
    args = parser.parse_args()
    urls = args.urls if args.urls is not None else os.environ.get('SOURCE_URLS', '')
    values = [part.strip() for part in urls.split(';') if part.strip()]
    if bool(values) == bool(args.archive):
        raise SystemExit('Supply either semicolon-separated source URLs or local --archive arguments')
    paths, identities = [], []
    for index, url in enumerate(values):
        path = args.downloads / f'source-{index + 1}.archive'
        identities.append(download(url, path))
        paths.append(path)
    merge_archives(paths or args.archive, args.output, args.manifest, identities or None)


if __name__ == '__main__':
    main()
