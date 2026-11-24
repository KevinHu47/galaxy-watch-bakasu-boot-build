"""GW8-only ABI generation regressions; no kernel, toolchain or make execution."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import compile_kernel_gw8
from abi_symbols_gw8 import BuildDefinition, prepare_abi_symbols
from collect_results import collect
from common import sha256


BUILD = '''
load("//build/kernel/kleaf:common_kernels.bzl", "define_common_kernels")
_VENDOR_LISTS = ["android/abi_gki_aarch64_exynos", "android/abi_gki_aarch64_other"]
filegroup(name = "vendor_lists", srcs = _VENDOR_LISTS)
define_common_kernels(target_configs = {
    "kernel_aarch64": {
        "kmi_symbol_list": "android/abi_gki_aarch64",
        "additional_kmi_symbol_lists": [":vendor_lists"],
        "module_implicit_outs": get_gki_modules_list("arm64"),
    },
    "kernel_aarch64_16k": {
        "kmi_symbol_list": "android/abi_gki_aarch64",
        "additional_kmi_symbol_lists": ["//common:android/abi_gki_aarch64_other"],
    },
})
'''
CONFIG = ('CONFIG_MODULE_SIG_PROTECT=y\nCONFIG_TRIM_UNUSED_KSYMS=y\n'
          'CONFIG_UNUSED_KSYMS_WHITELIST="abi_symbollist.raw"\n')


class AbiSymbolsGw8Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work / 'src/kernel-6.6'
        self.output = self.work / 'out'
        (self.source / 'android').mkdir(parents=True)
        self.output.mkdir()
        (self.source / 'BUILD.bazel').write_text(BUILD)
        (self.output / '.config').write_text(CONFIG)
        self.lists = {
            'abi_gki_aarch64': '[abi_symbol_list]\n  main_symbol\n  Shared\n',
            'abi_gki_aarch64_exynos': ('[abi_symbol_list]\r\n # device symbols\r\n'
                                     '  exynos_symbol\r\n  Shared\r\n'),
            'abi_gki_aarch64_other': ('[abi_whitelist]\n  another_vendor\n\n'
                                    '[metadata]\n  not_a_symbol = ignored\n'),
        }
        for name, contents in self.lists.items():
            (self.source / 'android' / name).write_bytes(contents.encode())
        self.report_path = self.work / 'abi-symbols-gw8.json'

    def generate(self):
        return prepare_abi_symbols(self.source, self.output, self.report_path)

    def test_missing_raw_is_generated_from_main_and_all_declared_vendor_lists(self):
        raw = self.output / 'abi_symbollist.raw'
        self.assertFalse(raw.exists())
        source_hashes = {path: sha256(path) for path in (self.source / 'android').iterdir()}
        before = (self.output / '.config').read_bytes()
        report = self.generate()
        self.assertEqual(raw.read_bytes(), b'Shared\nanother_vendor\nexynos_symbol\nmain_symbol\n')
        self.assertTrue(report['generated'])
        self.assertEqual(report['symbol_count'], 4)
        self.assertEqual(len(report['inputs']), 3)
        self.assertEqual(report['sha256'], sha256(raw))
        self.assertEqual(json.loads(self.report_path.read_text()), report)
        self.assertEqual(report['module_sig_protect'], 'y')
        self.assertEqual(report['trim_unused_ksyms'], 'y')
        self.assertEqual((self.output / '.config').read_bytes(), before)
        self.assertEqual(source_hashes, {path: sha256(path) for path in source_hashes})

    def test_uses_new_source_symbols_and_configured_output_path_instead_of_fixed_firmware_data(self):
        (self.output / '.config').write_text(CONFIG.replace('abi_symbollist.raw', 'generated/kmi.raw'))
        with (self.source / 'android/abi_gki_aarch64_exynos').open('a') as stream:
            stream.write('  future_firmware_symbol\n')
        report = self.generate()
        self.assertEqual(report['output'], 'generated/kmi.raw')
        self.assertIn('future_firmware_symbol\n', (self.output / report['output']).read_text())

    def test_selects_page_size_target_when_source_defines_it(self):
        with (self.output / '.config').open('a') as stream:
            stream.write('CONFIG_ARM64_16K_PAGES=y\n')
        report = self.generate()
        self.assertEqual(report['target'], 'kernel_aarch64_16k')
        self.assertNotIn('exynos_symbol', (self.output / report['output']).read_text())

    def test_supports_literal_globs_and_does_not_include_unlisted_files(self):
        (self.source / 'android/abi_gki_aarch64_unused').write_text('[abi_symbol_list]\nwrong_symbol\n')
        (self.source / 'BUILD.bazel').write_text(BUILD.replace(
            'srcs = _VENDOR_LISTS',
            'srcs = glob(["android/abi_gki_aarch64_*"], exclude = ["android/*_unused"])'))
        report = self.generate()
        self.assertNotIn('wrong_symbol', (self.output / report['output']).read_text())
        self.assertEqual(len(report['inputs']), 3)

    def test_missing_vendor_list_fails_instead_of_generating_partial_whitelist(self):
        (self.source / 'android/abi_gki_aarch64_exynos').unlink()
        with self.assertRaisesRegex(ValueError, 'symbol list is missing'):
            self.generate()
        self.assertFalse((self.output / 'abi_symbollist.raw').exists())

    def test_empty_symbol_union_is_rejected(self):
        for path in (self.source / 'android').iterdir():
            path.write_text('[abi_symbol_list]\n')
        with self.assertRaisesRegex(ValueError, 'no symbols'):
            self.generate()

    def test_unsupported_dynamic_build_expression_is_not_executed(self):
        (self.source / 'BUILD.bazel').write_text(BUILD.replace('srcs = _VENDOR_LISTS',
                                                              'srcs = arbitrary_code()'))
        with self.assertRaisesRegex(ValueError, 'Unsupported GW8 ABI BUILD expression'):
            self.generate()

    def test_cyclic_filegroup_is_rejected(self):
        (self.source / 'BUILD.bazel').write_text(BUILD.replace('srcs = _VENDOR_LISTS',
                                                              'srcs = [":vendor_lists"]'))
        with self.assertRaisesRegex(ValueError, 'Cyclic GW8 ABI filegroup'):
            self.generate()

    def test_output_and_input_traversal_are_rejected(self):
        (self.output / '.config').write_text(CONFIG.replace('abi_symbollist.raw', '../outside.raw'))
        with self.assertRaisesRegex(ValueError, 'inside the output directory'):
            self.generate()
        (self.output / '.config').write_text(CONFIG)
        (self.source / 'BUILD.bazel').write_text(BUILD.replace('android/abi_gki_aarch64_exynos', '../outside'))
        with self.assertRaisesRegex(ValueError, 'escapes the kernel tree'):
            self.generate()

    def test_no_configured_whitelist_is_not_replaced_with_an_invented_list(self):
        (self.output / '.config').write_text(CONFIG.replace('"abi_symbollist.raw"', '""'))
        (self.source / 'BUILD.bazel').unlink()
        report = self.generate()
        self.assertFalse(report['generated'])
        self.assertFalse((self.output / 'abi_symbollist.raw').exists())

    def test_direct_kernel_build_target_is_supported(self):
        (self.source / 'BUILD.bazel').write_text('''
kernel_build(name = "kernel_aarch64", kmi_symbol_list = "android/abi_gki_aarch64",
             additional_kmi_symbol_lists = ["android/abi_gki_aarch64_exynos"])
''')
        target, paths = BuildDefinition(self.source).symbol_lists({})
        self.assertEqual(target, 'kernel_aarch64')
        self.assertEqual(len(paths), 2)

    def test_compile_prepares_raw_before_make_prepare_and_image_modules(self):
        prepare = {'settings': {'source': str(self.source), 'kernel_release': 'test-release'},
                   'config_overrides': {}, 'bakasu': {'commit': 'test'}}
        (self.work / 'prepare.json').write_text(json.dumps(prepare))
        calls = []

        def make_stub(command, **kwargs):
            calls.append(command)
            if command[-1] in ('prepare', 'modules'):
                self.assertTrue((self.output / 'abi_symbollist.raw').is_file())
                self.assertTrue(self.report_path.is_file())
            if command[-1] == 'modules':
                image = self.output / 'arch/arm64/boot/Image'
                image.parent.mkdir(parents=True)
                image.write_bytes(b'test image')
                (self.output / 'System.map').write_text('test ksu_symbol\n')

        with (patch.object(sys, 'argv', ['compile_kernel_gw8.py', '--work', str(self.work), '--jobs', '2']),
              patch.object(compile_kernel_gw8, 'build_environment', return_value={}),
              patch.object(compile_kernel_gw8, 'configure_release_files'),
              patch.object(compile_kernel_gw8, 'check_config', return_value={'passed': True, 'other_changes': {}}),
              patch.object(compile_kernel_gw8.subprocess, 'run', side_effect=make_stub),
              patch.object(compile_kernel_gw8.subprocess, 'check_output', return_value='test-release\n'),
              patch.object(compile_kernel_gw8, 'verify_module_trust', return_value={'passed': True})):
            compile_kernel_gw8.main()
        self.assertEqual([command[-1] for command in calls], ['olddefconfig', 'prepare', 'modules'])
        self.assertEqual(calls[-1][-3:], ['-j2', 'Image', 'modules'])

    def test_new_report_is_collected_even_when_later_compilation_fails(self):
        self.generate()
        destination = self.work / 'dist'
        collect(self.work, destination)
        self.assertEqual((destination / self.report_path.name).read_bytes(), self.report_path.read_bytes())


if __name__ == '__main__':
    unittest.main()
