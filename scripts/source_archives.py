#!/usr/bin/env python3
"""Extract ordered Samsung source archives, including nested ZIP/tar wrappers.

The first discovered kernel package must be a complete tree. Later packages
overlay the same source tree. Archive member order determines nested-package
order; the semicolon-separated input determines top-level order.
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from common import download, sha256, write_json


def inside(root: Path, candidate: Path) -> bool:
    return candidate.resolve().is_relative_to(root.resolve())


def archive_kind(path: Path) -> str | None:
    if zipfile.is_zipfile(path):
        return 'zip'
    if tarfile.is_tarfile(path):
        return 'tar'
    return None


def safe_extract(path: Path, destination: Path) -> list[Path]:
    """Extract with path/link checks and return regular files in member order."""
    destination.mkdir(parents=True, exist_ok=False)
    kind = archive_kind(path)
    files = []
    if kind == 'tar':
        with tarfile.open(path, 'r:*') as archive:
            members = archive.getmembers()
            # data_filter rejects escaping names/links and special device files.
            archive.extractall(destination, members=members, filter='data')
            files = [destination / m.name for m in members if m.isfile()]
    elif kind == 'zip':
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                name = member.filename
                parts = PurePosixPath(name).parts
                if '\\' in name or name.startswith('/') or '..' in parts or (parts and ':' in parts[0]):
                    raise ValueError(f'Unsafe ZIP member: {name}')
                target = destination / name
                if not inside(destination, target):
                    raise ValueError(f'ZIP member escapes extraction directory: {name}')
                mode = member.external_attr >> 16
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                elif stat.S_ISLNK(mode):
                    link = archive.read(member).decode('utf-8')
                    if Path(link).is_absolute() or not inside(destination, target.parent / link):
                        raise ValueError(f'ZIP symlink escapes extraction directory: {name}')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(link)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as src, target.open('wb') as dst:
                        shutil.copyfileobj(src, dst)
                    target.chmod((stat.S_IMODE(mode) or 0o644) | 0o600)
                    files.append(target)
    else:
        raise ValueError(f'Expected ZIP or tar archive: {path.name}')
    # Samsung archives may store directories and sources read-only.
    for directory, subdirs, names in os.walk(destination, followlinks=False):
        current = Path(directory)
        current.chmod(current.stat().st_mode | 0o700)
        for name in names:
            item = current / name
            if not item.is_symlink():
                item.chmod(item.stat().st_mode | 0o600)
    return files


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
            subdirs[:] = []  # Do not mistake kernel/kernel or test fixtures for new roots.
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
    # Stop expanding once a source tree is found. Nested archives in a real
    # kernel source tree (e.g. test data) must remain untouched.
    for index, item in enumerate(files):
        if item.is_file() and not item.is_symlink() and archive_kind(item):
            found = True
            yield from packages(item, staging.parent / f'{staging.name}-nested-{index}',
                                origin + '!' + item.relative_to(staging).as_posix(), depth + 1)
    if not found:
        raise ValueError(f'No kernel source tree or nested source archive found in {origin}')


def merge_tree(source: Path, destination: Path, allowed_root: Path) -> dict:
    result = {'copied_files': 0, 'identical_files': 0, 'overwritten_files': []}
    destination.mkdir(parents=True, exist_ok=True)
    for directory, subdirs, files in os.walk(source, followlinks=False):
        parent = Path(directory)
        relative = parent.relative_to(source)
        target_dir = destination / relative
        if not inside(allowed_root, target_dir):
            raise ValueError(f'Merge destination escapes source workspace: {relative}')
        target_dir.mkdir(parents=True, exist_ok=True)
        target_dir.chmod(target_dir.stat().st_mode | 0o700)
        links = [name for name in subdirs if (parent / name).is_symlink()]
        subdirs[:] = [name for name in subdirs if name not in links and name != '.git']
        for name in files + links:
            item = parent / name
            target = target_dir / name
            if not inside(allowed_root, target.parent):
                raise ValueError(f'Merge destination escapes source workspace: {relative / name}')
            if item.is_symlink():
                link = item.readlink()
                if link.is_absolute() or not inside(allowed_root, target.parent / link):
                    raise ValueError(f'Relocated symlink escapes source workspace: {relative / name}')
                if target.is_symlink() and target.readlink() == link:
                    result['identical_files'] += 1
                    continue
                if target.exists() and target.is_dir() and not target.is_symlink():
                    raise ValueError(f'Symlink conflicts with a directory: {target}')
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(link)
            else:
                if target.is_symlink():
                    target.unlink()  # Never write through a previously merged symlink.
                if target.exists():
                    if not target.is_file():
                        raise ValueError(f'Source file conflicts with a directory: {target}')
                    before, after = sha256(target), sha256(item)
                    if before == after:
                        result['identical_files'] += 1
                        continue
                    result['overwritten_files'].append({
                        'path': (relative / name).as_posix(), 'before_sha256': before, 'after_sha256': after})
                    target.chmod(target.stat().st_mode | 0o600)
                shutil.copy2(item, target)
                target.chmod(target.stat().st_mode | 0o600)
            result['copied_files'] += 1
    return result


def merge_package(root: Path, output: Path, *, include_siblings: bool = True) -> dict:
    """Keep projects/, prebuilts/ and other package-level build inputs beside kernel/."""
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
            target.chmod(target.stat().st_mode | 0o600)
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
            print(f'Extracting source archive {index + 1}/{len(paths)}', flush=True)
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
