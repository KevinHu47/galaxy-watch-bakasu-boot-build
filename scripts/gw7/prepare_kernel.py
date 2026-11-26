#!/usr/bin/env python3
"""Recover the original build configuration and integrate a selected BakaSU Git revision."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import write_json
from kernel_prepare import (add_hooks, install_bakasu, prepare_config, prepare_toolchain,
                             read_assignments, read_config)
from kernel_release import release_settings


def build_settings(source: Path, stock: dict, stock_config: Path, source_manifest: Path) -> dict:
    constants = read_assignments(source / 'build.config.constants')
    makefile = read_assignments(source / 'Makefile')
    version = '.'.join(makefile[key] for key in ('VERSION', 'PATCHLEVEL', 'SUBLEVEL')) + makefile.get('EXTRAVERSION', '')
    release = stock['kernel_release']
    version_settings = release_settings(source, version, release, read_config(stock_config))
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
    return {**version_settings, 'branch': constants.get('BRANCH', ''),
            'clang_version': constants.get('CLANG_VERSION', ''),
            'kmi_generation': generation.group(1) if generation else '',
            'target_soc': socs[0], 'target_build_variant': 'user',
            'model_config_fragments_from_overlays': fragments}


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
