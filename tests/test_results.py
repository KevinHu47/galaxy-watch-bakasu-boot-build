import json
import sys
import tempfile
import unittest
from pathlib import Path

import lz4.frame

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from collect_results import collect
from common import sha256


class ResultsTests(unittest.TestCase):
    def test_artifact_allowlist_excludes_private_keys_and_marks_unsigned(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            work, output = root / 'work', root / 'dist'
            (work / 'out/certs').mkdir(parents=True)
            (work / 'out/certs/signing_key.pem').write_text('PRIVATE MODULE KEY')
            (work / 'avb-key.pem').write_text('PRIVATE AVB KEY')
            (work / 'boot.img').write_bytes(b'boot')
            (work / 'bakasu.json').write_text(json.dumps({'commit': 'a' * 40, 'repository': 'test'}))
            (work / 'boot-manifest.json').write_text(json.dumps({'verification': {'signed': False}}))
            collect(work, output, root / 'summary.md')
            self.assertIn('unsigned', (output / 'BUILD.txt').read_text())
            self.assertIn('BUILD.txt', (output / 'SHA256SUMS').read_text())
            self.assertIn('boot-unsign.img', (output / 'SHA256SUMS').read_text())
            self.assertIn('boot-unsign.img.lz4', (output / 'SHA256SUMS').read_text())
            self.assertEqual(lz4.frame.decompress((output / 'boot-unsign.img.lz4').read_bytes()), b'boot')
            for path in output.rglob('*'):
                if path.is_file():
                    self.assertNotIn(b'PRIVATE', path.read_bytes())

    def test_failed_early_build_still_has_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            work, output = root / 'work', root / 'dist'
            work.mkdir()
            output.mkdir()
            (work / 'boot.img').write_bytes(b'no verification manifest')
            (output / 'boot.img').write_bytes(b'previous build')
            (output / 'boot.img.lz4').write_bytes(b'previous build')
            collect(work, output)
            self.assertIn('incomplete', (output / 'BUILD.txt').read_text())
            self.assertFalse(list(output.glob('*.img*')))

    def test_lz4_contains_final_signed_or_unsigned_boot_and_its_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'dist'
            for index, (signed, name) in enumerate(((True, 'boot.img'), (False, 'boot-unsign.img'), (True, 'boot.img'))):
                work = root / f'work-{index}'
                work.mkdir()
                # Include nonzero content and the final footer/padding to catch
                # accidentally compressing Image or a pre-signing boot instead.
                final = bytes(range(256)) * 4096 + (b'AVBf' + bytes(60) if signed else bytes(64))
                (work / 'boot.img').write_bytes(final)
                (work / 'boot-manifest.json').write_text(json.dumps({'verification': {'signed': signed}}))
                collect(work, output)
                compressed_name = f'{name}.lz4'
                self.assertEqual({p.name for p in output.glob('*.img*')}, {name, compressed_name})
                self.assertEqual(json.loads((output / 'boot-manifest.json').read_text())['output'], name)
                compressed = (output / compressed_name).read_bytes()
                self.assertEqual(lz4.frame.decompress(compressed), final)
                info = lz4.frame.get_frame_info(compressed)
                self.assertEqual(info['content_size'], len(final))
                self.assertTrue(info['content_checksum'])
                self.assertFalse(info['block_linked'])
                self.assertFalse(info['block_checksum'])
                checksums = {name: digest for digest, name in
                             (line.split('  ', 1) for line in (output / 'SHA256SUMS').read_text().splitlines())}
                self.assertEqual(checksums[name], sha256(output / name))
                self.assertEqual(checksums[compressed_name], sha256(output / compressed_name))


if __name__ == '__main__':
    unittest.main()
