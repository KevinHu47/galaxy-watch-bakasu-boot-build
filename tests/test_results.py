import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            github_output = root / 'github-output.txt'
            with patch.dict('os.environ', {'GITHUB_OUTPUT': str(github_output), 'GITHUB_RUN_ID': '123'}):
                collect(work, output, root / 'summary.md')
            self.assertEqual(github_output.read_text(), 'artifact_name=boot-aaaaaaaaaaaa-unsign-123\n')
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

    def test_lz4_and_checksums_cover_each_published_boot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'dist'
            for signed in (True, False):
                work = root / f'work-{signed}'
                work.mkdir()
                # Include nonzero content and the final footer/padding to catch
                # accidentally compressing Image or a pre-signing boot instead.
                payload = bytes(range(256)) * 4096
                unsigned_boot = payload + bytes(64)
                expected = {'boot-unsign.img': unsigned_boot}
                if signed:
                    expected['boot.img'] = payload + b'AVBf' + bytes(60)
                    (work / 'boot-unsign.img').write_bytes(unsigned_boot)
                    (work / 'boot-unsign-manifest.json').write_text(json.dumps({'verification': {'signed': False}}))
                primary = 'boot.img' if signed else 'boot-unsign.img'
                (work / 'boot.img').write_bytes(expected[primary])
                (work / 'boot-manifest.json').write_text(json.dumps({'verification': {'signed': signed}}))
                collect(work, output)
                self.assertEqual({p.name for p in output.glob('*.img*')},
                                 set(expected) | {f'{name}.lz4' for name in expected})
                report = json.loads((output / 'boot-manifest.json').read_text())
                self.assertEqual(report['output'], primary)
                self.assertIs(report['verification']['signed'], signed)
                companion_report = output / 'boot-unsign-manifest.json'
                if signed:
                    report = json.loads(companion_report.read_text())
                    self.assertEqual(report['output'], 'boot-unsign.img')
                    self.assertIs(report['verification']['signed'], False)
                else:
                    self.assertFalse(companion_report.exists())
                checksums = {name: digest for digest, name in
                             (line.split('  ', 1) for line in (output / 'SHA256SUMS').read_text().splitlines())}
                for name, final in expected.items():
                    self.assertEqual((output / name).read_bytes(), final)
                    compressed_name = f'{name}.lz4'
                    compressed = (output / compressed_name).read_bytes()
                    self.assertEqual(lz4.frame.decompress(compressed), final)
                    info = lz4.frame.get_frame_info(compressed)
                    self.assertEqual(info['content_size'], len(final))
                    self.assertTrue(info['content_checksum'])
                    self.assertFalse(info['block_linked'])
                    self.assertFalse(info['block_checksum'])
                    self.assertEqual(checksums[name], sha256(output / name))
                    self.assertEqual(checksums[compressed_name], sha256(output / compressed_name))


if __name__ == '__main__':
    unittest.main()
