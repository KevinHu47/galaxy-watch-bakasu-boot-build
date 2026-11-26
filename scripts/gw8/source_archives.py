#!/usr/bin/env python3
"""Galaxy Watch 8 ONLY: merge versioned GKI and external Exynos source packages.

GW7 keeps using source_archives.py. Only its archive decoding and checked file
copy helpers are shared here; GW8 package discovery/layout is independent.
"""
from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from archive_utils import archive_kind, merge_tree, safe_extract
from common import download, sha256, write_json


def find_package(staging: Path) -> tuple[Path, Path | None, str] | None:
    """Return package root, optional GKI root, and complete/overlay classification."""
    kernels = []
    external = []
    for directory, subdirs, _ in os.walk(staging, followlinks=False):
        path = Path(directory)
        complete = all((path / name).is_file() for name in ('Makefile', 'Kconfig')) and (path / 'arch').is_dir()
        partial = (re.fullmatch(r'kernel-\d+(?:\.\d+)*', path.name) and
                   any((path / name).exists() for name in ('arch', 'drivers', 'include', 'Makefile')))
        if complete or partial:
            kernels.append((path, 'complete' if complete else 'overlay'))
            subdirs[:] = []
        elif path.name == 'exynos' and (path / 'device').is_dir():
            if any(p.name in ('Kbuild', 'Kconfig.ext') or p.parts[-2:] == ('arch', 'arm64')
                   for p in (path / 'device').rglob('*')):
                external.append(path.parent)
            subdirs[:] = []
    if len(kernels) > 1:
        raise ValueError('GW8 archive contains multiple GKI trees: ' +
                         ', '.join(str(path.relative_to(staging)) for path, _ in kernels))
    if kernels:
        kernel, kind = kernels[0]
        package = kernel.parent if kernel != staging else staging
        if any(root != package for root in external):
            raise ValueError('GW8 GKI and external device trees must belong to the same package root')
        return package, kernel, kind
    if len(external) > 1:
        raise ValueError('GW8 archive contains multiple external device packages')
    if external:
        return external[0], None, 'overlay'
    return None


def packages(path: Path, staging: Path, origin: str, depth: int = 0):
    if depth > 8:
        raise ValueError('More than eight nested source archive layers')
    files = safe_extract(path, staging)
    package = find_package(staging)
    if package:
        root, kernel, kind = package
        yield root, kernel, kind, {'member': origin, 'package_root': root.relative_to(staging).as_posix(),
                                  'kernel_root': kernel.relative_to(root).as_posix() if kernel else None}
        return
    found = False
    for index, item in enumerate(files):
        if item.is_file() and not item.is_symlink() and archive_kind(item):
            found = True
            yield from packages(item, staging.parent / f'{staging.name}-nested-{index}',
                                origin + '!' + item.relative_to(staging).as_posix(), depth + 1)
    if not found:
        raise ValueError(f'No GW8 GKI or external Exynos source package found in {origin}')


def merge_archives(paths: list[Path], output: Path, manifest: Path, identities: list[dict] | None = None):
    """Keep kernel-6.6/, exynos/, kernel/tests/, prebuilts/ at their native paths."""
    if output.exists():
        raise ValueError('GW8 source output already exists; use a fresh work directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'device_family': 'galaxy-watch8', 'kernel_tree': None, 'archives': [], 'packages': []}
    with tempfile.TemporaryDirectory(prefix='gw8-source-', dir=output.parent) as temp:
        for index, path in enumerate(paths):
            identity = identities[index] if identities else {'file': path.name, 'sha256': sha256(path)}
            report['archives'].append(identity)
            print(f'Extracting GW8 source archive {index + 1}/{len(paths)}', flush=True)
            for root, kernel, kind, record in packages(path, Path(temp) / f'archive-{index}', path.name):
                if not report['packages']:
                    if kind != 'complete' or kernel is None:
                        raise ValueError('The first GW8 package must contain a complete GKI kernel tree')
                    report['kernel_tree'] = record['kernel_root']
                elif kernel is not None and record['kernel_root'] != report['kernel_tree']:
                    raise ValueError('GW8 update kernel directory differs from the complete base package')
                record.update({'input_index': index + 1, 'kind': kind,
                               'destinations': sorted(p.name for p in root.iterdir() if p.is_dir()),
                               'merge': merge_tree(root, output, output)})
                report['packages'].append(record)
                write_json(manifest, report)
                print(f'  {kind}: {record["member"]} -> src/ (native layout; GKI={report["kernel_tree"]})', flush=True)
    if not report['kernel_tree'] or not (output / report['kernel_tree'] / 'Makefile').is_file():
        raise ValueError('No complete GW8 GKI kernel was produced')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, action='append')
    parser.add_argument('--output', type=Path, required=True, help='Workspace src/, not src/kernel/')
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--downloads', type=Path, required=True)
    args = parser.parse_args()
    urls = [value.strip() for value in os.environ.get('SOURCE_URLS', '').split(';') if value.strip()]
    if bool(urls) == bool(args.archive):
        raise SystemExit('Supply either SOURCE_URLS or local --archive arguments')
    paths, identities = [], []
    for index, url in enumerate(urls):
        path = args.downloads / f'source-{index + 1}.archive'
        identities.append(download(url, path))
        paths.append(path)
    merge_archives(paths or args.archive, args.output, args.manifest, identities or None)


if __name__ == '__main__':
    main()
