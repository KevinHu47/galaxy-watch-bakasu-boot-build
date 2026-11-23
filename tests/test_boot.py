import base64
import datetime
import gzip
import hashlib
import json
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import hashes
from cryptography import x509
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from boot_image import decode_signing_key, extract_config, package_boot, replace_kernel, verify_packaged


def image(size=8192, marker=b'original'):
    data = bytearray(size)
    data[56:60] = b'ARMd'
    data[128:128 + len(marker)] = marker
    return bytes(data)


def boot(kernel=None):
    kernel = kernel or image()
    header = bytearray(4096)
    header[:8] = b'ANDROID!'
    struct.pack_into('<4I', header, 8, len(kernel), 0, 1234, 1584)
    struct.pack_into('<I', header, 40, 4)
    # A synthetic Samsung-style opaque trailer must be preserved byte for byte.
    tail = bytes((-len(kernel)) % 4096) + b'SEANDROIDENFORCE' + b'opaque-original-tail' * 8
    original = bytes(header) + kernel + tail
    meta_offset = (len(original) + 4095) // 4096 * 4096
    result = bytearray(131072)
    result[:len(original)] = original
    result[meta_offset:meta_offset + 4] = b'AVB0'
    result[-64:] = struct.pack('>4s2I3Q28s', b'AVBf', 1, 0, len(original), meta_offset, 256, b'\0' * 28)
    return bytes(result)


class BootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        raw = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        cls.key_b64 = base64.b64encode(raw).decode()
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Original module signer')])
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(1).not_valid_before(datetime.datetime(2020, 1, 1))
                .not_valid_after(datetime.datetime(2040, 1, 1)).sign(key, hashes.SHA256()))
        cls.certificate = cert.public_bytes(serialization.Encoding.DER)

    def kernel_with_certificate(self, size):
        kernel = bytearray(image(size))
        kernel[1024:1024 + len(self.certificate)] = self.certificate
        return bytes(kernel)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stock = self.root / 'stock.img'
        self.stock.write_bytes(boot())
        self.image = self.root / 'Image'
        self.image.write_bytes(image(12288, b'new-compiled-kernel'))

    def test_unsigned_has_no_avb_and_preserves_samsung_tail(self):
        output = self.root / 'dist/boot.img'
        report = package_boot(self.stock, self.image, output, self.root / 'manifest.json', unsigned=True)
        expected, layout = replace_kernel(self.stock.read_bytes(), self.image.read_bytes())
        data = output.read_bytes()
        self.assertEqual(data[:len(expected)], expected)
        self.assertEqual(data[len(expected):], bytes(len(self.stock.read_bytes()) - len(expected)))
        self.assertEqual(len(data), len(self.stock.read_bytes()))
        self.assertEqual(len(data) % 4096, 0)
        self.assertNotEqual(output.read_bytes()[-64:-60], b'AVBf')
        self.assertFalse(report['verification']['signed'])
        self.assertIsNone(report['avb_algorithm'])
        self.assertTrue(report['verification']['same_size_as_original_boot'])
        self.assertTrue(report['verification']['file_size_4k_aligned'])
        self.assertFalse(report['verification']['avb_footer_present'])
        self.assertEqual(sorted(p.name for p in output.parent.iterdir()), ['boot.img'])

    @unittest.skipUnless(shutil.which('openssl'), 'openssl is required by avbtool')
    def test_signed_round_trip_and_no_standalone_vbmeta(self):
        output = self.root / 'dist/boot.img'
        report = package_boot(self.stock, self.image, output, self.root / 'manifest.json',
                              unsigned=False, key_b64=self.key_b64)
        self.assertEqual(len(output.read_bytes()), 131072)
        self.assertEqual(output.read_bytes()[-64:-60], b'AVBf')
        self.assertEqual(report['avb_algorithm'], 'SHA256_RSA4096')
        self.assertTrue(report['verification']['signed'])
        self.assertEqual(sorted(p.name for p in output.parent.iterdir()), ['boot.img'])
        self.assertEqual(report['avb_salt'], hashlib.sha256(self.image.read_bytes()).hexdigest())

    def test_tampered_kernel_or_tail_rejected(self):
        output = self.root / 'boot.img'
        package_boot(self.stock, self.image, output, self.root / 'manifest.json', unsigned=True)
        data = bytearray(output.read_bytes())
        data[4096 + 128] ^= 1
        output.write_bytes(data)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            verify_packaged(output, self.stock, self.image, signed=False)

    def test_original_module_certificate_preserved_in_both_signing_modes(self):
        self.stock.write_bytes(boot(self.kernel_with_certificate(8192)))
        self.image.write_bytes(self.kernel_with_certificate(12288))
        for unsigned in (True, False):
            if not unsigned and not shutil.which('openssl'):
                continue
            output = self.root / str(unsigned) / 'boot.img'
            report = package_boot(self.stock, self.image, output, self.root / f'{unsigned}.json',
                                  unsigned=unsigned, key_b64=self.key_b64)
            evidence = report['verification']['module_certificates']
            self.assertTrue(evidence['all_preserved'])
            self.assertEqual(evidence['original_module_certificate_sha256'],
                             [hashlib.sha256(self.certificate).hexdigest()])

    def test_missing_original_module_certificate_blocks_unsigned_packaging(self):
        self.stock.write_bytes(boot(self.kernel_with_certificate(8192)))
        with self.assertRaisesRegex(ValueError, 'module signing certificate missing'):
            package_boot(self.stock, self.image, self.root / 'boot.img', self.root / 'report.json', unsigned=True)
        self.assertFalse((self.root / 'boot.img').exists())

    def test_misaligned_replacement_rejected(self):
        with self.assertRaisesRegex(ValueError, 'alignment'):
            replace_kernel(boot(), image(8193))

    def test_kernel_replacement_discards_old_payload_instead_of_appending_boot(self):
        old_marker = b'UNIQUE_OLD_KERNEL_PAYLOAD'
        new_marker = b'UNIQUE_NEW_COMPILED_IMAGE'
        stock = boot(image(8192, old_marker))
        compiled = image(12288, new_marker)
        rebuilt, info = replace_kernel(stock, compiled)
        self.assertEqual(rebuilt[4096:4096 + len(compiled)], compiled)
        self.assertNotIn(old_marker, rebuilt)
        self.assertIn(new_marker, rebuilt)
        old_tail = stock[4096 + 8192:info['original_image_size']]
        self.assertEqual(rebuilt[4096 + len(compiled):], old_tail)
        self.assertEqual(len(rebuilt), 4096 + len(compiled) + len(old_tail))

    def test_whole_boot_concatenation_is_rejected_by_independent_verifier(self):
        output = self.root / 'wrong.img'
        output.write_bytes(self.stock.read_bytes() + self.image.read_bytes())
        with self.assertRaisesRegex(ValueError, 'does not match'):
            verify_packaged(output, self.stock, self.image, signed=False)

    def test_unsigned_extra_metadata_or_wrong_length_is_rejected(self):
        output = self.root / 'boot.img'
        package_boot(self.stock, self.image, output, self.root / 'report.json', unsigned=True)
        valid = output.read_bytes()
        wrong = bytearray(valid)
        wrong[-64:-60] = b'AVBf'
        output.write_bytes(wrong)
        with self.assertRaisesRegex(ValueError, 'zero padding'):
            verify_packaged(output, self.stock, self.image, signed=False)
        output.write_bytes(valid[:-4096])
        with self.assertRaisesRegex(ValueError, 'file size'):
            verify_packaged(output, self.stock, self.image, signed=False)

    def test_smaller_new_kernel_clears_old_payload_and_keeps_original_file_size(self):
        old_marker = b'OLD_KERNEL_BYTES_THAT_MUST_NOT_SURVIVE'
        old = bytearray(image(16384))
        old[14000:14000 + len(old_marker)] = old_marker
        self.stock.write_bytes(boot(bytes(old)))
        self.image.write_bytes(image(8192, b'new smaller kernel'))
        output = self.root / 'boot.img'
        package_boot(self.stock, self.image, output, self.root / 'report.json', unsigned=True)
        data = output.read_bytes()
        self.assertEqual(len(data), len(self.stock.read_bytes()))
        self.assertNotIn(old_marker, data)
        self.assertEqual(data[4096:4096 + 8192], self.image.read_bytes())

    def test_original_kernel_may_end_between_pages_with_marker_padding_preserved(self):
        # The validated Samsung Image ends 2560 bytes into a page. The existing
        # Samsung trailer supplies 1536 bytes of padding before its marker.
        original = boot(image(8192 + 2560))
        compiled = image(12288 + 2560)
        rebuilt, info = replace_kernel(original, compiled)
        self.assertNotEqual((4096 + info['kernel_size']) % 4096, 0)
        self.assertEqual(info['original_seandroid_marker_offset'] % 4096, 0)
        self.assertEqual(info['seandroid_marker_offset'] % 4096, 0)
        self.assertEqual(rebuilt[4096 + len(compiled):][:1536], bytes(1536))
        self.assertEqual(rebuilt[4096:4096 + len(compiled)], compiled)

    def test_replaced_payload_cannot_exceed_original_partition(self):
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            replace_kernel(boot(), image(131072))

    def test_original_boot_file_must_be_4k_aligned(self):
        with self.assertRaisesRegex(ValueError, 'file length'):
            replace_kernel(boot() + b'bad extension', image())

    def test_corrupt_avb_footer_bounds_rejected(self):
        data = bytearray(boot())
        struct.pack_into('>Q', data, len(data) - 64 + 12, len(data) + 1)
        with self.assertRaisesRegex(ValueError, 'footer bounds'):
            replace_kernel(bytes(data), image())

    def test_extract_actual_ikconfig_gzip_with_end_marker(self):
        config = b'# configuration\nCONFIG_COMPAT=y\nCONFIG_KSU=n\n'
        payload = image() + b'IKCFG_ST' + gzip.compress(config) + b'IKCFG_ED' + b'other bytes'
        self.assertEqual(extract_config(payload), config)
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            extract_config(payload.replace(b'IKCFG_ED', b'INVALID!'))

    def test_signing_secret_required_and_invalid_key_is_not_exposed(self):
        for value in ('', 'definitely-not-base64', base64.b64encode(b'SECRET_INVALID_PEM').decode()):
            with self.assertRaises(ValueError) as error:
                decode_signing_key(value, self.root / 'key.pem')
            self.assertNotIn('SECRET_INVALID_PEM', str(error.exception))
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        raw = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        with self.assertRaisesRegex(ValueError, '4096'):
            decode_signing_key(base64.b64encode(raw).decode(), self.root / 'key.pem')


if __name__ == '__main__':
    unittest.main()
