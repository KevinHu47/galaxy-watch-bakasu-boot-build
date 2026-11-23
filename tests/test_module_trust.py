import datetime
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from module_trust import parse_certificates, verify_module_trust


class ModuleTrustTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.certs = []
        for serial in (1, 2):
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f'Module signer {serial}')])
            cls.certs.append((x509.CertificateBuilder().subject_name(name).issuer_name(name)
                              .public_key(key.public_key()).serial_number(serial)
                              .not_valid_before(datetime.datetime(2020, 1, 1))
                              .not_valid_after(datetime.datetime(2040, 1, 1)).sign(key, hashes.SHA256())))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.der = [c.public_bytes(serialization.Encoding.DER) for c in self.certs]
        (self.root / 'stock.pem').write_bytes(self.certs[0].public_bytes(serialization.Encoding.PEM))
        (self.root / 'Image').write_bytes(b'compiled Image' + self.der[0])

    def elf(self, trusted):
        (self.root / 'trusted.bin').write_bytes(trusted)
        (self.root / 'outside.bin').write_bytes(self.der[0])
        asm = self.root / 'trust.S'
        asm.write_text('.section .rodata,"a"\n.global __cert_list_start\n.global __cert_list_end\n'
                       '__cert_list_start:\n.incbin "trusted.bin"\n__cert_list_end:\n'
                       '.section .outside,"a"\n.incbin "outside.bin"\n')
        result = self.root / 'vmlinux'
        subprocess.run(['gcc', '-c', str(asm), '-o', str(result)], cwd=self.root, check=True)
        return result

    def test_certificate_list_rejects_corrupt_or_truncated_data(self):
        self.assertEqual(parse_certificates(b''.join(self.der)), self.der)
        for blob in (b'not a certificate', self.der[0][:-1], b'\x30\x80'):
            with self.assertRaises(ValueError):
                parse_certificates(blob)

    @unittest.skipUnless(shutil.which('gcc'), 'gcc is needed for the ELF fixture')
    def test_actual_builtin_list_and_Image_are_both_required(self):
        vmlinux = self.elf(self.der[0])
        result = verify_module_trust(vmlinux, self.root / 'Image', self.root / 'stock.pem', self.root / 'report.json')
        self.assertTrue(result['passed'])
        (self.root / 'Image').write_bytes(b'certificate was removed')
        with self.assertRaisesRegex(ValueError, 'absent'):
            verify_module_trust(vmlinux, self.root / 'Image', self.root / 'stock.pem', self.root / 'report.json')
        self.assertFalse(json.loads((self.root / 'report.json').read_text())['passed'])

    @unittest.skipUnless(shutil.which('gcc'), 'gcc is needed for the ELF fixture')
    def test_arbitrary_embedded_certificate_outside_trust_list_cannot_pass(self):
        vmlinux = self.elf(self.der[1])
        self.assertIn(self.der[0], vmlinux.read_bytes())
        with self.assertRaisesRegex(ValueError, 'absent'):
            verify_module_trust(vmlinux, self.root / 'Image', self.root / 'stock.pem', self.root / 'report.json')
        report = json.loads((self.root / 'report.json').read_text())
        self.assertTrue(report['original_module_certificates'][0]['in_compiled_Image'])
        self.assertFalse(report['original_module_certificates'][0]['in_builtin_trust_list'])


if __name__ == '__main__':
    unittest.main()
