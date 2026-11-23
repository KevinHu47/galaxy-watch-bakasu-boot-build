#!/usr/bin/env python3
"""Recover the original build configuration and integrate a selected BakaSU Git revision."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from common import download, sha256, write_json
from source_archives import safe_extract

DEFAULT_REPOSITORY = 'https://github.com/Baka-SU/BakaSU.git'
OVERRIDES = {
    'KSU': 'y', 'KSU_TRACEPOINT_HOOK': 'y', 'KSU_MULTI_MANAGER_SUPPORT': 'y',
    'KSU_MANUAL_HOOK': 'n', 'KSU_SUSFS': 'n',
    'UH': 'n', 'RKP': 'n', 'KDP': 'n', 'KDP_CRED': 'n',
    'SECURITY_DEFEX': 'n', 'SECURITY_DEFEX_USER': 'n', 'SECURITY_DEFEX_SINGLE_FILE': 'n',
    'KSU_FULL_NAME_FORMAT': '%TAG_NAME%-%COMMIT_SHA%',
}


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


def repository_url(value: str) -> str:
    value = value.strip() or DEFAULT_REPOSITORY
    if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
        value = 'https://github.com/' + value
    if not re.fullmatch(r'https://[A-Za-z0-9.-]+/[A-Za-z0-9_./-]+', value):
        raise ValueError('BakaSU repository must be owner/repo or an HTTPS Git repository URL')
    return value.rstrip('/')


def install_bakasu(source: Path, repository: str, revision: str) -> dict:
    repository = repository_url(repository)
    revision = revision.strip()
    if revision.startswith('-') or '\n' in revision or '\r' in revision:
        raise ValueError('Invalid BakaSU commit/ref')
    target = source / 'BakaSU'
    if target.exists():
        raise ValueError('Source already contains BakaSU; use a clean source work directory')
    subprocess.run(['git', 'clone', '--no-checkout', '--', repository, str(target)], check=True)
    if revision:
        # Fetch also allows an explicit reachable commit not named by a branch.
        subprocess.run(['git', '-C', str(target), 'fetch', 'origin', revision], check=True)
        selected = 'FETCH_HEAD'
    else:
        selected = 'refs/remotes/origin/HEAD'
    commit = git(target, 'rev-parse', '--verify', selected + '^{commit}')
    subprocess.run(['git', '-C', str(target), 'checkout', '--detach', commit], check=True)
    if not (target / 'kernel/Kconfig').is_file():
        raise ValueError('Selected repository/ref does not contain BakaSU kernel/Kconfig')
    print(f'BakaSU repository: {repository}', flush=True)
    print(f'BakaSU commit: {commit}', flush=True)
    return {'repository': repository, 'requested_ref': revision or None, 'commit': commit,
            'default_branch': git(target, 'symbolic-ref', 'refs/remotes/origin/HEAD'),
            'commit_count': int(git(target, 'rev-list', '--count', 'HEAD')),
            'commit_subject': git(target, 'show', '-s', '--format=%s', 'HEAD')}


def read_assignments(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*[:?+]?=\s*(.*?)\s*$', line)
        if match:
            values[match[1]] = match[2].strip('"\'')
    return values


def read_config(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if line.startswith('CONFIG_') and '=' in line:
            key, value = line.split('=', 1)
            values[key] = value
        elif re.fullmatch(r'# CONFIG_\w+ is not set', line):
            values[line.split()[1]] = 'n'
    return values


def add_hooks(source: Path):
    link = source / 'drivers/kernelsu'
    if link.exists() or link.is_symlink():
        raise ValueError('Source already has drivers/kernelsu; supply original Samsung sources')
    link.symlink_to('../BakaSU/kernel', target_is_directory=True)
    path = source / 'drivers/Makefile'
    text = path.read_text()
    line = 'obj-$(CONFIG_KSU) += kernelsu/'
    if line not in text:
        path.write_text(text.rstrip() + '\n\n' + line + '\n')
    path = source / 'drivers/Kconfig'
    text = path.read_text()
    line = 'source "drivers/kernelsu/Kconfig"'
    if line not in text:
        if 'endmenu' not in text:
            raise ValueError('drivers/Kconfig has no endmenu insertion point')
        path.write_text(text.replace('endmenu', line + '\nendmenu', 1))


def build_settings(source: Path, stock: dict, stock_config: Path, source_manifest: Path) -> dict:
    constants = read_assignments(source / 'build.config.constants')
    makefile = read_assignments(source / 'Makefile')
    version = '.'.join(makefile[key] for key in ('VERSION', 'PATCHLEVEL', 'SUBLEVEL')) + makefile.get('EXTRAVERSION', '')
    release = stock['kernel_release']
    if not release.startswith(version) or (len(release) > len(version) and release[len(version)].isdigit()):
        raise ValueError(f'Source kernel {version} does not match original boot kernel {release}; use matching sources')
    config = read_config(stock_config)
    localversion = config.get('CONFIG_LOCALVERSION', '""').strip('"')
    if not release.startswith(version + localversion):
        raise ValueError('Cannot reconstruct the original kernel release suffix')
    suffix = release[len(version + localversion):]
    (source / '.scmversion').write_text(suffix + '\n')
    # TARGET_PRODUCT only affected disabled DEFEX in the validated Samsung source tree.
    # Regions/screen sizes are retained in the original config and supplied
    # sources. No fixed product, region or screen size is imposed here.
    projects = source.parent / 'projects'
    socs = sorted({value for p in projects.glob('*/build.config.*.common')
                   if re.fullmatch(r'[a-zA-Z0-9_]+', value := read_assignments(p).get('TARGET_SOC', ''))})
    if len(socs) != 1:
        raise ValueError('Cannot determine one TARGET_SOC from source projects/*/build.config.*.common')
    # Log model-specific configuration fragments contributed by input overlays.
    manifest = json.loads(source_manifest.read_text())
    fragments = []
    for package in manifest['packages']:
        for row in package['merge']['kernel']['overwritten_files']:
            if row['path'].startswith('arch/arm64/configs/'):
                fragments.append(row['path'])
    generation = re.search(r'-android\d+-(\d+)', release)
    return {'kernel_release': release, 'kernel_source_version': version,
            'release_suffix': suffix, 'branch': constants.get('BRANCH', ''),
            'clang_version': constants.get('CLANG_VERSION', ''),
            'kmi_generation': generation.group(1) if generation else '',
            'target_soc': socs[0], 'target_build_variant': 'user',
            'model_config_fragments_from_overlays': fragments}


def prepare_toolchain(source: Path, work: Path, settings: dict, override: str) -> dict:
    version = settings['clang_version']
    if not re.fullmatch(r'r[0-9]+[a-z0-9-]*', version):
        raise ValueError('Source build.config.constants does not select a valid CLANG_VERSION')
    branch = settings['branch']
    android = re.search(r'android\d+', branch)
    if not override and not android:
        raise ValueError('Cannot derive Android toolchain branch; provide toolchain_url')
    url = override or ('https://android.googlesource.com/platform/prebuilts/clang/host/linux-x86/'
                       f'+archive/refs/heads/{android.group()}-release/clang-{version}.tar.gz')
    identity = download(url, work / 'clang.archive')
    toolchain = work / 'toolchain'
    safe_extract(work / 'clang.archive', toolchain)
    candidates = [p.parent.parent for p in toolchain.rglob('bin/clang') if p.is_file()]
    if len(candidates) != 1:
        raise ValueError('Toolchain archive must contain one bin/clang (nested directories are allowed)')
    root = candidates[0]
    result = subprocess.check_output([str(root / 'bin/clang'), '--version'], text=True)
    print(result, flush=True)
    (work / 'clang.archive').unlink()  # Its identity is retained; free runner disk for BTF/swap.
    return {**identity, 'root': str(root), 'version_output': result.strip(), 'source_clang_version': version}


def prepare_config(source: Path, stock_dir: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(stock_dir / 'stock.config', output / '.config')
    cert = stock_dir / 'stock-module-keys.pem'
    overrides = dict(OVERRIDES)
    if cert.stat().st_size:
        shutil.copyfile(cert, source / 'certs/gw-stock-gki.pem')
        overrides['SYSTEM_TRUSTED_KEYS'] = 'certs/gw-stock-gki.pem'
    args = ['bash', str(source / 'scripts/config'), '--file', str(output / '.config')]
    for name, value in overrides.items():
        if value in ('y', 'n'):
            args.extend(['--enable' if value == 'y' else '--disable', name])
        else:
            args.extend(['--set-str', name, value])
    subprocess.run(args, check=True)
    return overrides


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--stock-dir', type=Path, required=True)
    parser.add_argument('--source-manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repository', default=os.environ.get('BAKASU_REPOSITORY', ''))
    parser.add_argument('--ref', default=os.environ.get('BAKASU_REF', ''))
    parser.add_argument('--toolchain-url', default=os.environ.get('TOOLCHAIN_URL', ''))
    args = parser.parse_args()
    stock = json.loads((args.stock_dir / 'stock-boot.json').read_text())
    settings = build_settings(args.source, stock, args.stock_dir / 'stock.config', args.source_manifest)
    bakasu = install_bakasu(args.source, args.repository, args.ref)
    # Write the resolved commit immediately, so it is retained if compilation fails.
    write_json(args.work / 'bakasu.json', bakasu)
    add_hooks(args.source)
    overrides = prepare_config(args.source, args.stock_dir, args.output)
    toolchain = prepare_toolchain(args.source, args.work, settings, args.toolchain_url)
    manifest = {'settings': settings, 'bakasu': bakasu, 'toolchain': toolchain,
                'config_overrides': overrides}
    write_json(args.work / 'prepare.json', manifest)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write(f'bakasu_commit={bakasu["commit"]}\n')
    print(f'Prepared original configuration for {settings["kernel_release"]}; SoC {settings["target_soc"]}')


if __name__ == '__main__':
    main()
