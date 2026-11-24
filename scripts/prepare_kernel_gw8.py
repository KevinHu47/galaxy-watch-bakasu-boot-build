#!/usr/bin/env python3
"""Galaxy Watch 8 ONLY: resolve Android 15 GKI and external Exynos Kconfig paths.

GW7 keeps using prepare_kernel.py. BakaSU checkout, compiler extraction and
root/config insertion use its existing helpers without changing their behavior.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from common import write_json
from kernel_release import release_settings
from prepare_kernel import (add_hooks, install_bakasu, prepare_config, prepare_toolchain,
                            read_assignments, read_config)


def build_settings(source: Path, stock: dict, stock_config: Path, source_manifest: Path) -> dict:
    constants = {}
    for name in ('build.config.common', 'build.config.constants'):
        constants.update(read_assignments(source / name))
    makefile = read_assignments(source / 'Makefile')
    version = '.'.join(makefile[key] for key in ('VERSION', 'PATCHLEVEL', 'SUBLEVEL')) + makefile.get('EXTRAVERSION', '')
    release = stock['kernel_release']
    version_settings = release_settings(source, version, release, read_config(stock_config))
    workspace = source.parent
    soc_configs = []
    for path in (workspace / 'exynos/soc-series').glob('*/build.config.*'):
        values = read_assignments(path)
        if re.fullmatch(r'[A-Za-z0-9_]+', values.get('EXYNOS_SOC', '')):
            soc_configs.append((path, values))
    soc_choices = {(p.parent, values['EXYNOS_SOC']) for p, values in soc_configs}
    if len(soc_choices) != 1:
        raise ValueError('Cannot determine a unique GW8 Exynos SoC configuration')
    soc_dir, soc = next(iter(soc_choices))
    boards = [p.parent for p in (workspace / 'exynos/device').glob('*/*/Kconfig.ext')]
    if len(boards) != 1:
        raise ValueError('Cannot determine a unique GW8 external board Kconfig')
    board = boards[0]
    external_modules = workspace / 'exynos/external-modules'
    if not (external_modules / 'sgpu/Kconfig').is_file():
        raise ValueError('GW8 external module Kconfig files are missing')
    environment = {
        'KCONFIG_EXT_PREFIX': os.path.relpath(board, source) + '/',
        'KCONFIG_EXT_SOC_PREFIX': os.path.relpath(soc_dir, source) + '/',
        'KCONFIG_EXT_MODULES_PREFIX': os.path.relpath(external_modules, source) + '/',
    }
    manifest = json.loads(source_manifest.read_text())
    fragments = [row['path'] for package in manifest['packages']
                 for row in package['merge']['overwritten_files'] if '/arch/arm64/configs/' in row['path']]
    generation = re.search(r'-android\d+-(\d+)', release)
    return {**version_settings, 'device_family': 'galaxy-watch8', 'source': str(source),
            'branch': constants.get('BRANCH', ''), 'clang_version': constants.get('CLANG_VERSION', ''),
            'kmi_generation': generation.group(1) if generation else constants.get('KMI_GENERATION', ''),
            'target_soc': soc, 'target_build_variant': 'user', 'kconfig_environment': environment,
            'model_config_fragments_from_overlays': fragments}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--repository', default=os.environ.get('BAKASU_REPOSITORY', ''))
    parser.add_argument('--ref', default=os.environ.get('BAKASU_REF', ''))
    parser.add_argument('--toolchain-url', default=os.environ.get('TOOLCHAIN_URL', ''))
    args = parser.parse_args()
    work = args.work.resolve()
    source_report = json.loads((work / 'source-manifest.json').read_text())
    source = work / 'src' / source_report['kernel_tree']
    if not source.resolve().is_relative_to((work / 'src').resolve()):
        raise ValueError('GKI source path is outside the GW8 source workspace')
    stock = json.loads((work / 'stock/stock-boot.json').read_text())
    settings = build_settings(source, stock, work / 'stock/stock.config', work / 'source-manifest.json')
    bakasu = install_bakasu(source, args.repository, args.ref)
    write_json(work / 'bakasu.json', bakasu)
    add_hooks(source)
    overrides = prepare_config(source, work / 'stock', work / 'out')
    toolchain = prepare_toolchain(source, work, settings, args.toolchain_url)
    write_json(work / 'prepare.json', {'settings': settings, 'bakasu': bakasu, 'toolchain': toolchain,
                                      'config_overrides': overrides})
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write(f'bakasu_commit={bakasu["commit"]}\n')
    print(f'Prepared GW8 {settings["kernel_release"]}; {settings["branch"]}; {settings["clang_version"]}')


if __name__ == '__main__':
    main()
