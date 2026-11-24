"""Release metadata tests only: no kernel checkout, download or compilation."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from kernel_release import STAMP_FILE, configure_release_files, release_settings


class KernelReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()

    def test_kernel_and_android_versions_are_input_values_not_constants(self):
        for version, stamp in [('6.1.134', '-android14-11'), ('6.6.150', '-android15-12-newbuild'),
                               ('6.12.42', '-android16-3-futurebuild')]:
            with self.subTest(version=version):
                result = release_settings(self.source, version, version + stamp, {'CONFIG_LOCALVERSION': '""'})
                self.assertEqual(result['kernel_source_version'], version)
                self.assertEqual(result['release_suffix'], stamp)
                self.assertEqual(result['release_prepend'], '')

    def test_page_size_is_preserved_without_pin_to_4k(self):
        for page in ('-4k', '-16k', '-64k'):
            with self.subTest(page=page):
                release = '6.12.42-android16-3-newbuild' + page
                result = release_settings(self.source, '6.12.42', release, {'CONFIG_LOCALVERSION': '"' + page + '"'})
                self.assertEqual(result['release_prepend'], '-android16-3-newbuild')
                self.assertEqual(result['release_suffix'], '')
                self.assertEqual(result['config_localversion'], page)

    def test_historical_config_prefix_order_is_still_supported(self):
        result = release_settings(self.source, '6.1.134', '6.1.134-regional-android14-11',
                                  {'CONFIG_LOCALVERSION': '"-regional"'})
        self.assertEqual(result['release_prepend'], '')
        self.assertEqual(result['release_suffix'], '-android14-11')

    def test_source_localversion_files_and_backup_files(self):
        (self.source / 'localversion.1').write_text('-vendor\n')
        (self.source / 'localversion.1~').write_text('ignored')
        result = release_settings(self.source, '6.6.89', '6.6.89-build-vendor-4k',
                                  {'CONFIG_LOCALVERSION': '"-4k"'})
        self.assertEqual(result['source_localversion'], '-vendor')
        self.assertEqual(result['release_prepend'], '-build')

    def test_only_a_real_base_version_mismatch_is_rejected(self):
        for release in ('6.6.90-new', '6.6.890-new'):
            with self.subTest(release=release), self.assertRaisesRegex(ValueError, 'does not match'):
                release_settings(self.source, '6.6.89', release, {'CONFIG_LOCALVERSION': '""'})

    def test_inconsistent_config_suffix_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'preserving'):
            release_settings(self.source, '6.6.89', '6.6.89-build-16k', {'CONFIG_LOCALVERSION': '"-4k"'})

    def test_output_stamp_is_idempotent_and_preserves_config(self):
        output = self.root / 'out'
        output.mkdir()
        original = 'CONFIG_LOCALVERSION="-4k"\n'
        (output / '.config').write_text(original)
        (self.source / '.scmversion').write_text('-old-cache')
        settings = release_settings(self.source, '6.6.89', '6.6.89-build-4k', {'CONFIG_LOCALVERSION': '"-4k"'})
        configure_release_files(self.source, output, settings)
        configure_release_files(self.source, output, settings)
        self.assertEqual((output / STAMP_FILE).read_text(), '-build\n')
        self.assertEqual((self.source / '.scmversion').read_text(), '')
        self.assertEqual((output / '.config').read_text(), original)
        # A retry with a prefix-style version cannot retain a previous prepend.
        other = release_settings(self.source, '6.6.89', '6.6.89-4k-build', {'CONFIG_LOCALVERSION': '"-4k"'})
        configure_release_files(self.source, output, other)
        self.assertFalse((output / STAMP_FILE).exists())


if __name__ == '__main__':
    unittest.main()
