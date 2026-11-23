#!/usr/bin/env python3
"""Collect only public build results; produce a commit-addressed Actions summary."""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from common import sha256, write_json


def compress_boot(source: Path, destination: Path) -> None:
    import lz4.frame

    original = source.read_bytes()
    compressed = lz4.frame.compress(
        original, block_size=lz4.frame.BLOCKSIZE_MAX1MB, block_linked=False,
        content_checksum=True, block_checksum=False, store_size=True,
    )
    if lz4.frame.decompress(compressed) != original:
        raise ValueError(f'{destination.name} does not decompress to {source.name}')
    destination.write_bytes(compressed)


def collect(work: Path, destination: Path, summary: Path | None = None):
    destination.mkdir(parents=True, exist_ok=True)
    reports = ['source-manifest.json', 'bakasu.json', 'prepare.json', 'config-check.json',
               'compile.json', 'module-trust.json', 'boot-manifest.json']
    for name in reports:
        if (work / name).is_file():
            shutil.copyfile(work / name, destination / name)
    for name in ['stock-boot.json', 'stock.config', 'stock-module-keys.pem']:
        if (work / 'stock' / name).is_file():
            # These are recovered public certificates only, never private keys.
            output_name = 'stock-module-keys.txt' if name.endswith('.pem') else name
            shutil.copyfile(work / 'stock' / name, destination / output_name)
    for name in ['Module.symvers', 'System.map', '.config']:
        if (work / 'out' / name).is_file():
            shutil.copyfile(work / 'out' / name, destination / ('kernel.config' if name == '.config' else name))
    image = work / 'out/arch/arm64/boot/Image'
    if image.is_file():
        shutil.copyfile(image, destination / 'Image')
    bakasu = json.loads((work / 'bakasu.json').read_text()) if (work / 'bakasu.json').exists() else {}
    boot = json.loads((work / 'boot-manifest.json').read_text()) if (work / 'boot-manifest.json').exists() else {}
    signed = boot.get('verification', {}).get('signed')
    source_boot = work / 'boot.img'
    boot_name = None
    mode = 'incomplete'
    if source_boot.is_file() and isinstance(signed, bool):
        boot_name = 'boot.img' if signed else 'boot-unsign.img'
        mode = 'signed' if signed else 'unsigned'

    # A reused output directory must not mix images from different modes.
    for name in ('boot.img', 'boot-unsign.img'):
        if name != boot_name:
            (destination / name).unlink(missing_ok=True)
            (destination / f'{name}.lz4').unlink(missing_ok=True)

    boot_hash = 'not produced'
    if boot_name is not None:
        published_boot = destination / boot_name
        shutil.copyfile(source_boot, published_boot)
        compress_boot(published_boot, destination / f'{boot_name}.lz4')
        boot_hash = sha256(published_boot)
        boot['output'] = boot_name
        write_json(destination / 'boot-manifest.json', boot)

    text = ['# Galaxy Watch BakaSU boot', '',
            f'- BakaSU repository: `{bakasu.get("repository", "not resolved")}`',
            f'- BakaSU commit: `{bakasu.get("commit", "not resolved")}`',
            f'- Result: **{mode}**',
            f'- boot SHA-256: `{boot_hash}`', '']
    if signed is False:
        text.extend(['Unsigned build (not recommended): no new AVB metadata or signature; '
                     'the original Samsung tail and recovered module certificates are preserved; '
                     'zero-padded to the original boot file size.', ''])
    if summary:
        with summary.open('a') as stream:
            stream.write('\n'.join(text))
    (destination / 'BUILD.txt').write_text('\n'.join(text), encoding='utf-8')
    hashes = {p.name: sha256(p) for p in destination.iterdir() if p.is_file() and p.name != 'SHA256SUMS'}
    (destination / 'SHA256SUMS').write_text(''.join(f'{value}  {name}\n' for name, value in sorted(hashes.items())))
    if os.environ.get('GITHUB_OUTPUT'):
        commit = bakasu.get('commit', 'unresolved')[:12]
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write(f'artifact_name=gw-boot-{commit}-{mode}-{os.environ.get("GITHUB_RUN_ID", "local")}\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    collect(args.work, args.output, Path(os.environ['GITHUB_STEP_SUMMARY']) if os.environ.get('GITHUB_STEP_SUMMARY') else None)
