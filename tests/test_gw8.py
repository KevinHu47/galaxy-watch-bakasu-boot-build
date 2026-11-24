"""GW8-only layout/settings tests. These do not download or compile a kernel."""
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from source_archives_gw8 import merge_archives
from prepare_kernel_gw8 import build_settings
from compile_kernel_gw8 import build_environment


def archive(files):
    tar_data = io.BytesIO()
    with tarfile.open(fileobj=tar_data, mode='w:gz') as tar:
        for name, value in files.items():
            value = value.encode()
            entry = tarfile.TarInfo(name)
            entry.size = len(value)
            tar.addfile(entry, io.BytesIO(value))
    zip_data = io.BytesIO()
    with zipfile.ZipFile(zip_data, 'w') as zipped:
        zipped.writestr('source_kernel.tar.gz', tar_data.getvalue())
    return zip_data.getvalue()


class Gw8Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'src/kernel-6.6'

    def base_files(self):
        return {'kernel-6.6/Makefile': 'VERSION = 6\nPATCHLEVEL = 6\nSUBLEVEL = 89\nEXTRAVERSION =\n',
                'kernel-6.6/Kconfig': 'mainmenu "test"\n',
                'kernel-6.6/arch/arm64/configs/gki_defconfig': 'CONFIG_COMPAT=y\n',
                'kernel-6.6/build.config.constants': 'CLANG_VERSION=r510928\n',
                'kernel-6.6/build.config.common': 'BRANCH=android15-6.6\nKMI_GENERATION=8\n',
                'kernel-6.6/drivers/usb/gadget/function/android_f_accessory.c': 'old usb',
                'kernel/tests/net/test/example.py': 'source test fixture',
                'exynos/device/erd/erd5535/Kconfig.ext': 'board config',
                'exynos/soc-series/s-wear6/build.config.s5e5535_w_b': 'EXYNOS_SOC="s5e5535"\n',
                'exynos/soc-series/s-wear6/Kconfig.ext': 'soc config',
                'exynos/external-modules/sgpu/Kconfig': 'module config'}

    def write_archives(self, *contents):
        paths = []
        for index, files in enumerate(contents):
            path = self.root / f'archive-{index}.zip'
            path.write_bytes(archive(files))
            paths.append(path)
        return paths

    def merge(self, *contents):
        return merge_archives(self.write_archives(*contents), self.root / 'src', self.root / 'manifest.json')

    def settings(self, release='6.6.89-android15-8'):
        config = self.root / 'stock.config'
        config.write_text('CONFIG_LOCALVERSION=""\n')
        return build_settings(self.source, {'kernel_release': release}, config, self.root / 'manifest.json')

    def test_three_packages_retain_native_gki_and_external_device_paths(self):
        board = 'exynos/device/erd/erd5535/'
        files = self.base_files()
        files[board + 'arch/arm64/configs/wise8bsue_00_defconfig'] = 'base'
        second = {'Kernel/' + board + 'arch/arm64/configs/wise8bsue_00_defconfig': 'model',
                  'Kernel/' + board + 'Kbuild': 'model build',
                  'Kernel/prebuilts/platform/erd5535/firmware.bin': 'firmware'}
        third = {'Kernel/kernel-6.6/drivers/usb/gadget/function/android_f_accessory.c': 'new usb',
                 'Kernel/' + board + 'drivers/battery/battery/sec_battery.c': 'new battery'}
        report = self.merge(files, second, third)
        self.assertEqual(report['kernel_tree'], 'kernel-6.6')
        self.assertIsNone(report['packages'][1]['kernel_root'])
        self.assertEqual((self.root / 'src/kernel/tests/net/test/example.py').read_text(), 'source test fixture')
        self.assertFalse((self.root / 'src/kernel/Makefile').exists())
        self.assertEqual((self.source / 'drivers/usb/gadget/function/android_f_accessory.c').read_text(), 'new usb')
        self.assertEqual((self.root / 'src' / board / 'drivers/battery/battery/sec_battery.c').read_text(), 'new battery')
        self.assertFalse((self.source / 'arch/arm64/configs/wise8bsue_00_defconfig').exists())
        self.assertEqual(self.settings()['model_config_fragments_from_overlays'],
                         [board + 'arch/arm64/configs/wise8bsue_00_defconfig'])

    def test_settings_read_external_soc_and_common_branch_and_keep_original_suffix(self):
        self.merge(self.base_files())
        settings = self.settings('6.6.89-android15-8-abcdef-ab123')
        self.assertEqual(settings['branch'], 'android15-6.6')
        self.assertEqual(settings['clang_version'], 'r510928')
        self.assertEqual(settings['target_soc'], 's5e5535')
        self.assertEqual(settings['release_suffix'], '-android15-8-abcdef-ab123')
        self.assertEqual(settings['kconfig_environment'], {
            'KCONFIG_EXT_PREFIX': '../exynos/device/erd/erd5535/',
            'KCONFIG_EXT_SOC_PREFIX': '../exynos/soc-series/s-wear6/',
            'KCONFIG_EXT_MODULES_PREFIX': '../exynos/external-modules/'})
        self.assertFalse((self.source / '.scmversion').exists())

    def test_environment_supplies_external_kconfigs_and_excludes_signing_key(self):
        self.merge(self.base_files())
        settings = self.settings()
        env = build_environment({'settings': settings, 'toolchain': {'root': '/tools'}},
                                {'PATH': '/usr/bin', 'AVB_PRIVATE_KEY_B64': 'secret',
                                 'KCONFIG_EXT_PREFIX': 'wrong/', 'KBUILD_EXTMOD': 'wrong'})
        self.assertEqual(env['LOCALVERSION'], '-android15-8')
        self.assertEqual(env['KCONFIG_EXT_PREFIX'], '../exynos/device/erd/erd5535/')
        self.assertEqual(env['PATH'], '/tools/bin:/usr/bin')
        self.assertNotIn('AVB_PRIVATE_KEY_B64', env)
        self.assertNotIn('KBUILD_EXTMOD', env)

    def test_samsung_page_size_suffix_stays_after_android_firmware_stamp(self):
        self.merge(self.base_files())
        config = self.root / 'stock.config'
        original = 'CONFIG_LOCALVERSION="-4k"\nCONFIG_LOCALVERSION_AUTO=y\nCONFIG_ARM64_4K_PAGES=y\n'
        config.write_text(original)
        release = '6.6.89-android15-8-abL500XXS2AZF4-4k'
        settings = build_settings(self.source, {'kernel_release': release}, config, self.root / 'manifest.json')
        self.assertEqual(settings['kernel_release'], release)
        self.assertEqual(settings['release_prepend'], '-android15-8-abL500XXS2AZF4')
        self.assertEqual(settings['release_suffix'], '')
        self.assertEqual(config.read_text(), original)
        env = build_environment({'settings': settings, 'toolchain': {'root': '/tools'}}, {'PATH': '/usr/bin'})
        self.assertEqual(env['LOCALVERSION'], '')

    def test_external_package_cannot_be_first(self):
        with self.assertRaisesRegex(ValueError, 'first GW8 package'):
            self.merge({'Kernel/exynos/device/erd/erd5535/Kconfig.ext': 'only device'})

    def test_conflicting_kernel_version_overlay_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.merge(self.base_files(), {'Kernel/kernel-6.1/drivers/Makefile': 'wrong version'})

    def test_multiple_external_boards_are_rejected(self):
        files = self.base_files()
        files['exynos/device/erd/other/Kconfig.ext'] = 'ambiguous'
        self.merge(files)
        with self.assertRaisesRegex(ValueError, 'unique GW8 external board'):
            self.settings()

    def test_gw8_workflow_uses_its_own_sources_preparation_and_compilation(self):
        repo = Path(__file__).resolve().parents[1]
        workflow = (repo / '.github/workflows/build-boot-gw8.yml').read_text()
        for filename in ('source_archives_gw8.py', 'prepare_kernel_gw8.py', 'compile_kernel_gw8.py'):
            self.assertIn(filename, workflow)
        self.assertNotIn('python3 scripts/compile_kernel.py', workflow)
        self.assertIn('--output "$WORK/src"', workflow)
        self.assertIn('work/gw8-build', workflow)
        self.assertIn('dist/gw8/', workflow)
        # Generic boot packaging and module trust remain shared unchanged.
        self.assertIn('python3 scripts/boot_image.py pack', workflow)
        self.assertNotIn('make_vbmeta_image', workflow)


if __name__ == '__main__':
    unittest.main()
