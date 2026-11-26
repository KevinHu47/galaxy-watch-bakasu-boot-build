import io
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from gw7.source_archives import merge_archives
from archive_utils import safe_extract


def make_tar(files, *, links=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, value in files.items():
            entry = tarfile.TarInfo(name)
            data = value.encode() if isinstance(value, str) else value
            entry.size = len(data)
            entry.mode = 0o444
            archive.addfile(entry, io.BytesIO(data))
        for name, target in (links or {}).items():
            entry = tarfile.TarInfo(name)
            entry.type = tarfile.SYMTYPE
            entry.linkname = target
            archive.addfile(entry)
    return buffer.getvalue()


def make_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return buffer.getvalue()


class SourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, name, contents):
        path = self.root / name
        path.write_bytes(contents)
        return path

    def complete(self, prefix='kernel/'):
        return {prefix + 'Makefile': 'VERSION = 6\n', prefix + 'Kconfig': 'menu "Kernel"\n',
                prefix + 'arch/arm64/configs/device_defconfig': 'CONFIG_COMPAT=y\n',
                prefix + 'drivers/Makefile': 'base\n', prefix + 'drivers/Kconfig': 'endmenu\n'}

    def test_nested_sources_apply_in_input_order_and_keep_package_siblings(self):
        base = self.complete()
        base['projects/s5e5535/build.config'] = 'soc'
        base['kernel/tools/test-fixture.tar.gz'] = b'leave test fixture alone'
        first = self.archive('base.bin', make_zip({'wrapper/source.tar.gz': make_tar(base)}))
        second = self.archive('device.zip', make_zip({'nested/device.tar.gz': make_tar({
            'Kernel/kernel/arch/arm64/boot/dts/device.dts': 'model',
            'Kernel/kernel/drivers/Makefile': 'device\n',
            'Kernel/prebuilts/firmware.bin': b'firmware',
        })}))
        third = self.archive('update.tgz', make_tar({'Release/Kernel/kernel/drivers/Makefile': 'update\n'}))
        out = self.root / 'build/src/kernel'
        report = merge_archives([first, second, third], out, self.root / 'manifest.json')
        self.assertEqual((out / 'drivers/Makefile').read_text(), 'update\n')
        self.assertEqual((out / 'arch/arm64/boot/dts/device.dts').read_text(), 'model')
        self.assertEqual((out.parent / 'projects/s5e5535/build.config').read_text(), 'soc')
        self.assertEqual((out.parent / 'prebuilts/firmware.bin').read_bytes(), b'firmware')
        self.assertEqual((out / 'tools/test-fixture.tar.gz').read_bytes(), b'leave test fixture alone')
        self.assertEqual(len(report['packages']), 3)
        self.assertEqual(report['packages'][1]['merge']['kernel']['overwritten_files'][0]['path'], 'drivers/Makefile')

    def test_outer_bundle_preserves_member_order(self):
        bundle = self.archive('bundle.zip', make_zip({
            'base.zip': make_zip({'base.tar.gz': make_tar(self.complete())}),
            'model.zip': make_zip({'model.tar.gz': make_tar({'Kernel/kernel/drivers/Makefile': 'model'})}),
            'update.zip': make_zip({'update.tar.gz': make_tar({'Kernel/kernel/drivers/Makefile': 'latest'})}),
        }))
        out = self.root / 'build/src/kernel'
        merge_archives([bundle], out, self.root / 'manifest.json')
        self.assertEqual((out / 'drivers/Makefile').read_text(), 'latest')

    def test_root_level_tree_does_not_copy_extraction_parent(self):
        first = self.archive('base.tgz', make_tar(self.complete(prefix='')))
        out = self.root / 'build/src/kernel'
        merge_archives([first], out, self.root / 'manifest.json')
        self.assertEqual(sorted(p.name for p in out.parent.iterdir()), ['kernel'])

    def test_overlay_cannot_be_first(self):
        first = self.archive('overlay.tgz', make_tar({'Kernel/kernel/drivers/Makefile': 'overlay'}))
        with self.assertRaisesRegex(ValueError, 'first source package'):
            merge_archives([first], self.root / 'build/src/kernel', self.root / 'manifest.json')

    def test_fourth_package_with_only_kernel_makefile_is_applied(self):
        first = self.archive('base.tgz', make_tar(self.complete()))
        overlays = [self.archive(f'update-{i}.tgz', make_tar({'Kernel/kernel/Makefile': f'update-{i}'}))
                    for i in range(3)]
        out = self.root / 'build/src/kernel'
        merge_archives([first, *overlays], out, self.root / 'manifest.json')
        self.assertEqual((out / 'Makefile').read_text(), 'update-2')

    def test_ambiguous_complete_trees_are_rejected(self):
        files = self.complete('a/kernel/') | self.complete('b/kernel/')
        first = self.archive('ambiguous.tar.gz', make_tar(files))
        with self.assertRaisesRegex(ValueError, 'multiple kernel roots'):
            merge_archives([first], self.root / 'build/src/kernel', self.root / 'manifest.json')

    def test_zip_path_traversal_rejected(self):
        first = self.archive('escape.zip', make_zip({'../outside.txt': 'bad'}))
        with self.assertRaisesRegex(ValueError, 'Unsafe ZIP'):
            safe_extract(first, self.root / 'extract')
        self.assertFalse((self.root / 'outside.txt').exists())

    def test_tar_path_and_symlink_traversal_rejected(self):
        for index, data in enumerate((make_tar({'../outside.txt': 'bad'}),
                                      make_tar({}, links={'escape': '../../outside'}))):
            first = self.archive(f'escape-{index}.tar.gz', data)
            with self.assertRaises(tarfile.FilterError):
                safe_extract(first, self.root / f'extract-{index}')

    def test_relative_source_symlink_preserved(self):
        first = self.archive('base.tar.gz', make_tar(self.complete(), links={'kernel/include-alias': 'arch'}))
        out = self.root / 'build/src/kernel'
        merge_archives([first], out, self.root / 'manifest.json')
        self.assertTrue((out / 'include-alias').is_symlink())
        self.assertEqual((out / 'include-alias').readlink(), Path('arch'))


if __name__ == '__main__':
    unittest.main()
