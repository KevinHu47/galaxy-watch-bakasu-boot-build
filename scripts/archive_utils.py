"""Shared safe archive extraction and checked source-tree merge helpers."""
from __future__ import annotations

import os
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

from common import sha256


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
    for directory, subdirs, names in os.walk(destination, followlinks=False):
        current = Path(directory)
        current.chmod(current.stat().st_mode | 0o700)
        for name in names:
            item = current / name
            if not item.is_symlink():
                item.chmod(item.stat().st_mode | 0o600)
    return files


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
                    target.unlink()
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
