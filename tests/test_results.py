import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from collect_results import collect


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
            self.assertIn('boot.img', (output / 'SHA256SUMS').read_text())
            for path in output.rglob('*'):
                if path.is_file():
                    self.assertNotIn(b'PRIVATE', path.read_bytes())

    def test_failed_early_build_still_has_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            collect(root / 'absent-work', root / 'dist')
            self.assertIn('incomplete', (root / 'dist/BUILD.txt').read_text())
            self.assertFalse((root / 'dist/boot.img').exists())


if __name__ == '__main__':
    unittest.main()
