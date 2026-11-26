#!/usr/bin/env python3
"""Galaxy Watch 8 ONLY: compile GKI with the external Exynos Kconfig environment.

GW7 keeps using compile_kernel.py. Config comparison and module-certificate
verification are reused unchanged; kernel path and version handling are GW8-only.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gw8.abi_symbols import prepare_abi_symbols
from common import sha256, write_json
from kernel_release import configure_release_files, localversion_residual
from kernel_build import check_config
from module_trust import verify_module_trust


def build_environment(prepare: dict, inherited: dict | None = None) -> dict:
    settings = prepare['settings']
    env = dict(inherited if inherited is not None else os.environ)
    for name in ('AVB_PRIVATE_KEY_B64', 'BUILD_NUMBER', 'KBUILD_OUTPUT', 'KCONFIG_CONFIG',
                 'LOCALVERSION', 'TARGET_PRODUCT', 'KBUILD_EXTMOD', 'KCONFIG_EXT_PREFIX',
                 'KCONFIG_EXT_SOC_PREFIX', 'KCONFIG_EXT_MODULES_PREFIX'):
        env.pop(name, None)
    env.update({'PATH': prepare['toolchain']['root'] + '/bin:' + env['PATH'],
                'ARCH': 'arm64', 'LLVM': '1', 'LLVM_IAS': '1',
                'CROSS_COMPILE': 'aarch64-linux-gnu-', 'CROSS_COMPILE_COMPAT': 'arm-linux-gnueabi-',
                'KBUILD_BUILD_USER': 'build-user', 'KBUILD_BUILD_HOST': 'build-host',
                'KBUILD_BUILD_VERSION': '1', 'KBUILD_BUILD_TIMESTAMP': 'Thu Jan  1 00:00:00 UTC 1970',
                'SOURCE_DATE_EPOCH': '0', 'KCONFIG_NOTIMESTAMP': '1',
                'BRANCH': settings['branch'], 'KMI_GENERATION': settings['kmi_generation'],
                'TARGET_SOC': settings['target_soc'], 'TARGET_BUILD_VARIANT': settings['target_build_variant'],
                'KCFLAGS': '-D__ANDROID_COMMON_KERNEL__', 'LOCALVERSION': settings['release_suffix']})
    env['GIT_CEILING_DIRECTORIES'] = str(Path(prepare['settings']['source']).resolve().parents[1])
    env.update(settings['kconfig_environment'])
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=max(1, os.cpu_count() or 2))
    args = parser.parse_args()
    work = args.work.resolve()
    prepare = json.loads((work / 'prepare.json').read_text())
    source, output = Path(prepare['settings']['source']), work / 'out'
    if not source.resolve().is_relative_to((work / 'src').resolve()):
        raise ValueError('GW8 kernel source is outside the build workspace')
    env = build_environment(prepare)
    command = ['make', '-C', str(source), f'O={output}', 'ARCH=arm64', 'LLVM=1', 'LLVM_IAS=1']
    prepare_abi_symbols(source, output, work / 'abi-symbols-gw8.json')
    release_detection = None
    release = None
    source_version = prepare['settings'].get('kernel_source_version')
    if not source_version:
        source_version = prepare['settings']['kernel_release'].split('-', 1)[0]
    for include_prepend in ([False, True] if prepare['settings'].get('release_prepend') else [False]):
        configure_release_files(source, output, prepare['settings'], include_prepend=include_prepend)
        env['LOCALVERSION'] = ''
        subprocess.run([*command, 'olddefconfig'], env=env, check=True)
        report = check_config(work / 'stock/stock.config', output / '.config', prepare['config_overrides'])
        write_json(work / 'config-check.json', report)
        if not report['passed']:
            raise ValueError('GW8 required root/stock settings changed; see config-check.json')
        if report['other_changes']:
            print('Additional GW8 Kconfig changes recorded:', ', '.join(report['other_changes']), flush=True)
        subprocess.run([*command, 'prepare'], env=env, check=True)
        probe = subprocess.check_output([*command, '--no-print-directory', '-s', 'kernelrelease'],
                                        env=env, text=True).strip().splitlines()[-1]
        try:
            residual = localversion_residual(source_version,
                                             prepare['settings']['kernel_release'], probe)
        except ValueError:
            continue
        env['LOCALVERSION'] = residual
        subprocess.run([*command, 'prepare'], env=env, check=True)
        release = subprocess.check_output([*command, '--no-print-directory', '-s', 'kernelrelease'],
                                          env=env, text=True).strip().splitlines()[-1]
        if release == prepare['settings']['kernel_release']:
            release_detection = {'probe': probe, 'include_prepend': include_prepend,
                                 'localversion': residual,
                                 'git_ceiling_directories': env.get('GIT_CEILING_DIRECTORIES', '')}
            break
    if release != prepare['settings']['kernel_release']:
        raise ValueError(f'GW8 release {release!r} differs from original {prepare["settings"]["kernel_release"]!r}')
    subprocess.run([*command, f'-j{max(1, args.jobs)}', 'Image', 'modules'], env=env, check=True)
    image = output / 'arch/arm64/boot/Image'
    if not image.is_file() or 'ksu_' not in (output / 'System.map').read_text():
        raise ValueError('GW8 Image or BakaSU symbols are missing')
    trust = verify_module_trust(output / 'vmlinux', image, work / 'stock/stock-module-keys.pem',
                                work / 'module-trust.json')
    write_json(work / 'compile.json', {'device_family': 'galaxy-watch8', 'kernel_release': release,
                                     'image_sha256': sha256(image), 'image_bytes': image.stat().st_size,
                                     'bakasu': prepare['bakasu'], 'has_bakasu_symbols': True,
                                     'original_module_trust_verified': trust['passed'],
                                     'release_detection': release_detection})


if __name__ == '__main__':
    main()
