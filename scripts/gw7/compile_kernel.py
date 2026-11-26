#!/usr/bin/env python3
"""Compile with the recovered stock config and retain its kernel release."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import sha256, write_json
from kernel_release import configure_release_files, localversion_residual
from module_trust import verify_module_trust
from kernel_build import check_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=max(1, os.cpu_count() or 2))
    args = parser.parse_args()
    work = args.work
    source, output = work / 'src/kernel', work / 'out'
    prepare = json.loads((work / 'prepare.json').read_text())
    settings = prepare['settings']
    env = os.environ.copy()
    # The signing secret is provided to a later workflow step only.
    env.pop('AVB_PRIVATE_KEY_B64', None)
    env.update({'PATH': prepare['toolchain']['root'] + '/bin:' + env['PATH'],
                'ARCH': 'arm64', 'LLVM': '1', 'LLVM_IAS': '1',
                'CROSS_COMPILE': 'aarch64-linux-gnu-', 'CROSS_COMPILE_COMPAT': 'arm-linux-gnueabi-',
                'KBUILD_BUILD_USER': 'build-user', 'KBUILD_BUILD_HOST': 'build-host',
                'KBUILD_BUILD_VERSION': '1', 'KBUILD_BUILD_TIMESTAMP': 'Thu Jan  1 00:00:00 UTC 1970',
                'SOURCE_DATE_EPOCH': '0', 'KCONFIG_NOTIMESTAMP': '1',
                'BRANCH': settings['branch'], 'KMI_GENERATION': settings['kmi_generation'],
                'TARGET_BUILD_VARIANT': settings['target_build_variant'], 'TARGET_SOC': settings['target_soc'],
                'KCFLAGS': '-D__ANDROID_COMMON_KERNEL__'})
    # The downloaded Samsung source lives below this checkout in the normal
    # workflow. Prevent setlocalversion from treating the workflow commit as
    # the kernel's SCM suffix.
    env['GIT_CEILING_DIRECTORIES'] = str(work.resolve())
    for key in ('BUILD_NUMBER', 'KBUILD_OUTPUT', 'KCONFIG_CONFIG', 'LOCALVERSION', 'TARGET_PRODUCT'):
        env.pop(key, None)
    command = ['make', '-C', str(source), f'O={output}', 'ARCH=arm64', 'LLVM=1', 'LLVM_IAS=1']
    release_probe = None
    release_detection = None
    # First let the source's own setlocalversion provide any Android/KMI
    # fragment. If that is insufficient, add the input-derived prepend stamp
    # and probe again. The final LOCALVERSION is only the missing suffix.
    attempts = [False] + ([True] if settings.get('release_prepend') else [])
    for include_prepend in attempts:
        configure_release_files(source, output, settings, include_prepend=include_prepend)
        env['LOCALVERSION'] = ''
        subprocess.run([*command, 'olddefconfig'], env=env, check=True)
        report = check_config(work / 'stock/stock.config', output / '.config', prepare['config_overrides'])
        write_json(work / 'config-check.json', report)
        if not report['passed']:
            raise ValueError('Required root/stock ABI settings changed; see config-check.json')
        if report['other_changes']:
            print('Additional Kconfig changes are recorded in config-check.json:',
                  ', '.join(report['other_changes']), flush=True)
        subprocess.run([*command, 'prepare'], env=env, check=True)
        probe = subprocess.check_output([*command, '--no-print-directory', '-s', 'kernelrelease'],
                                        env=env, text=True).strip().splitlines()[-1]
        try:
            residual = localversion_residual(settings['kernel_source_version'],
                                             settings['kernel_release'], probe)
        except ValueError:
            release_probe = probe
            continue
        env['LOCALVERSION'] = residual
        subprocess.run([*command, 'prepare'], env=env, check=True)
        release = subprocess.check_output([*command, '--no-print-directory', '-s', 'kernelrelease'],
                                          env=env, text=True).strip().splitlines()[-1]
        if release == settings['kernel_release']:
            release_detection = {'probe': probe, 'include_prepend': include_prepend,
                                 'localversion': residual,
                                 'git_ceiling_directories': env['GIT_CEILING_DIRECTORIES']}
            break
        release_probe = release
    else:
        raise ValueError('Built kernel release differs from original: '
                         f'expected={settings["kernel_release"]!r}, observed={release_probe!r}')
    subprocess.run([*command, f'-j{max(1, args.jobs)}', 'Image', 'modules'], env=env, check=True)
    image = output / 'arch/arm64/boot/Image'
    if not image.is_file():
        raise ValueError('Build did not produce arch/arm64/boot/Image')
    symbols = (output / 'System.map').read_text()
    if 'ksu_' not in symbols:
        raise ValueError('Built kernel has no BakaSU symbols')
    trust = verify_module_trust(output / 'vmlinux', image, work / 'stock/stock-module-keys.pem',
                                work / 'module-trust.json')
    write_json(work / 'compile.json', {'kernel_release': release, 'image_sha256': sha256(image),
                                     'image_bytes': image.stat().st_size, 'bakasu': prepare['bakasu'],
                                     'has_bakasu_symbols': True, 'original_module_trust_verified': trust['passed'],
                                     'release_detection': release_detection})
    print(f'Compiled Image: {sha256(image)}', flush=True)


if __name__ == '__main__':
    main()
