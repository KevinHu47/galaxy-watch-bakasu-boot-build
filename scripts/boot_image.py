#!/usr/bin/env python3
"""Read a Samsung boot template, recover build inputs, and package only boot.img."""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import os
import re
import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from common import download, sha256, write_json


def normalize_boot(data: bytes) -> bytes:
    if data.startswith(b'ANDROID!'):
        return data
    if data.startswith(b'\x1f\x8b'):
        return normalize_boot(gzip.decompress(data))
    if data.startswith(b'\x04\x22\x4d\x18'):
        import lz4.frame
        return normalize_boot(lz4.frame.decompress(data))
    raise ValueError('The boot link must download an Android boot image (.img, .img.gz or .img.lz4)')


def layout(data: bytes) -> dict:
    if len(data) < 4096 or data[:8] != b'ANDROID!':
        raise ValueError('Missing Android boot image header')
    version = struct.unpack_from('<I', data, 40)[0]
    # Parse the template's actual format; there is no model/region allowlist.
    if version not in (3, 4):
        raise ValueError(f'Cannot parse boot header v{version} using the supported Android boot image layout')
    kernel_size, ramdisk_size, _, header_size = struct.unpack_from('<4I', data, 8)
    result = {'header_version': version, 'header_size': header_size, 'page_size': 4096,
              'kernel_size': kernel_size, 'ramdisk_size': ramdisk_size,
              'kernel_offset': 4096, 'kernel_end': 4096 + kernel_size,
              'file_size_4k_aligned': len(data) % 4096 == 0,
              'gki_signature_size': struct.unpack_from('<I', data, 1580)[0] if version == 4 else 0,
              'partition_size': len(data), 'original_image_size': len(data), 'has_avb_footer': False}
    if data[-64:-60] == b'AVBf':
        footer = struct.unpack_from('>4s2I3Q28s', data, len(data) - 64)
        result.update(original_image_size=footer[3], vbmeta_offset=footer[4],
                      vbmeta_size=footer[5], has_avb_footer=True)
        if not (0 < footer[3] <= footer[4] and footer[4] + footer[5] <= len(data) - 64):
            raise ValueError('Invalid original boot AVB footer bounds')
        if footer[4] % 4096:
            raise ValueError('Original boot AVB metadata offset is not 4 KiB aligned')
    if not (0 < kernel_size <= result['original_image_size'] - 4096):
        raise ValueError('Kernel payload extends beyond the original boot image')
    if not (1580 <= header_size <= 4096):
        raise ValueError('Invalid Android boot header size')
    return result


def extract_config(kernel: bytes) -> bytes:
    start = kernel.find(b'IKCFG_ST\x1f\x8b')
    if start < 0:
        raise ValueError('Original boot kernel does not contain IKCONFIG; provide an original boot with embedded configuration')
    decoder = zlib.decompressobj(wbits=31)
    data = decoder.decompress(kernel[start + 8:])
    if not decoder.eof or not decoder.unused_data.startswith(b'IKCFG_ED') or b'CONFIG_' not in data:
        raise ValueError('Original kernel IKCONFIG is incomplete')
    return data


def kernel_release(kernel: bytes) -> str:
    match = re.search(rb'Linux version ([A-Za-z0-9_.+\-]+)', kernel)
    if not match:
        raise ValueError('Cannot find the Linux version string in the original kernel')
    return match.group(1).decode('ascii')


def is_self_signed(certificate) -> bool:
    if certificate.subject != certificate.issuer:
        return False
    key = certificate.public_key()
    try:
        if isinstance(key, rsa.RSAPublicKey):
            key.verify(certificate.signature, certificate.tbs_certificate_bytes,
                       padding.PKCS1v15(), certificate.signature_hash_algorithm)
        elif isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(certificate.signature, certificate.tbs_certificate_bytes,
                       ec.ECDSA(certificate.signature_hash_algorithm))
        else:
            return False
    except Exception:
        return False
    return True


def module_certificates(kernel: bytes) -> tuple[bytes, list[dict]]:
    """Recover embedded self-signed kernel keys; exclude separately issued FIVE certs."""
    offset, pem, report, seen = 0, bytearray(), [], set()
    while True:
        offset = kernel.find(b'\x30\x82', offset)
        if offset < 0:
            break
        size = int.from_bytes(kernel[offset + 2:offset + 4], 'big') + 4
        der = kernel[offset:offset + size]
        try:
            cert = x509.load_der_x509_certificate(der)
        except ValueError:
            offset += 1
            continue
        fingerprint = cert.fingerprint(hashes.SHA256()).hex()
        selected = is_self_signed(cert)
        report.append({'offset': offset, 'sha256_der': fingerprint,
                       'subject': cert.subject.rfc4514_string(), 'selected_self_signed_key': selected})
        if selected and fingerprint not in seen:
            seen.add(fingerprint)
            pem.extend(cert.public_bytes(serialization.Encoding.PEM))
        offset += size
    return bytes(pem), report


def inspect_boot(boot: Path, work: Path, *, url: str | None = None) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    identity = download(url, work / 'boot-download.bin') if url else {'file': boot.name, 'sha256': sha256(boot)}
    raw = (work / 'boot-download.bin').read_bytes() if url else boot.read_bytes()
    data = normalize_boot(raw)
    info = layout(data)
    kernel = data[4096:4096 + info['kernel_size']]
    if kernel[56:60] != b'ARMd':
        raise ValueError('Cannot read an uncompressed ARM64 Image from the original boot')
    config = extract_config(kernel)
    certificates, cert_report = module_certificates(kernel)
    if not certificates and any(value in config for value in
                                (b'CONFIG_MODULE_SIG_PROTECT=y', b'CONFIG_MODULE_SIG_FORCE=y')):
        raise ValueError('Original kernel requires trusted modules, but no self-signed module key could be recovered')
    (work / 'stock-boot.img').write_bytes(data)
    (work / 'stock-Image').write_bytes(kernel)
    (work / 'stock.config').write_bytes(config)
    (work / 'stock-module-keys.pem').write_bytes(certificates)
    info.update({'download': identity, 'sha256': hashlib.sha256(data).hexdigest(),
                 'kernel_sha256': hashlib.sha256(kernel).hexdigest(),
                 'kernel_release': kernel_release(kernel), 'certificates': cert_report})
    write_json(work / 'stock-boot.json', info)
    print(f'Original kernel: {info["kernel_release"]}; boot SHA-256: {info["sha256"]}')
    print(f'Recovered {sum(c["selected_self_signed_key"] for c in cert_report)} self-signed kernel module key(s)')
    return info


def replace_kernel(template: bytes, kernel: bytes) -> tuple[bytes, dict]:
    info = layout(template)
    if not info['file_size_4k_aligned']:
        raise ValueError('Original boot file length is not 4 KiB aligned; supply the complete original boot image')
    if kernel[56:60] != b'ARMd':
        raise ValueError('Compiled payload is not an ARM64 Image')
    # A pre-existing v4 GKI signature authenticates the old Image; preserving it
    # would misrepresent the new payload. Templates used by this packer have this field zero.
    if info['gki_signature_size']:
        raise ValueError('Template contains a separate GKI boot signature that must be rebuilt before kernel replacement')
    old_end = 4096 + info['kernel_size']
    tail = template[old_end:info['original_image_size']]
    if (len(kernel) - info['kernel_size']) % 4096:
        raise ValueError('New Image changes the template page alignment; cannot preserve its tail correctly')
    output = bytearray(template[:4096]) + kernel + tail
    if len(output) > info['partition_size']:
        raise ValueError('Replaced boot payload exceeds the original boot partition size')
    struct.pack_into('<I', output, 8, len(kernel))
    marker = output.find(b'SEANDROIDENFORCE', 4096 + len(kernel))
    old_marker = template.find(b'SEANDROIDENFORCE', old_end, info['original_image_size'])
    if old_marker >= 0 and marker % 4096 != old_marker % 4096:
        raise ValueError('Samsung tail alignment changed')
    info.update({'new_kernel_size': len(kernel), 'new_kernel_offset': 4096,
                 'new_kernel_end': 4096 + len(kernel), 'unsigned_image_size': len(output),
                 'tail_size': len(tail), 'tail_sha256': hashlib.sha256(tail).hexdigest(),
                 'original_seandroid_marker_offset': old_marker,
                 'seandroid_marker_offset': marker,
                 'seandroid_4k_alignment_preserved': marker < 0 or marker % 4096 == old_marker % 4096})
    return bytes(output), info


def decode_signing_key(value: str, target: Path) -> None:
    if not value.strip():
        raise ValueError('Signed build requires the AVB_PRIVATE_KEY_B64 repository secret')
    try:
        raw = base64.b64decode(''.join(value.split()), validate=True)
        key = serialization.load_pem_private_key(raw, password=None)
    except (ValueError, TypeError):
        raise ValueError('AVB_PRIVATE_KEY_B64 must contain base64 of an unencrypted PEM RSA private key') from None
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 4096:
        raise ValueError('Use a 4096-bit RSA key for SHA256_RSA4096 boot signing')
    target.write_bytes(raw)
    target.chmod(0o600)


def verify_preserved_module_certificates(stock_kernel: bytes, new_kernel: bytes) -> dict:
    """Check certificate bytes in the final payload independently of AVB signing."""
    bundle, _ = module_certificates(stock_kernel)
    expected = x509.load_pem_x509_certificates(bundle) if bundle else []
    fingerprints, missing = [], []
    for cert in expected:
        fingerprint = cert.fingerprint(hashes.SHA256()).hex()
        fingerprints.append(fingerprint)
        if cert.public_bytes(serialization.Encoding.DER) not in new_kernel:
            missing.append(fingerprint)
    if missing:
        raise ValueError('Original module signing certificate missing from final boot kernel: ' + ', '.join(missing))
    return {'original_module_certificate_sha256': fingerprints, 'all_preserved': True}


def verify_packaged(output: Path, stock: Path, image: Path, *, signed: bool) -> dict:
    data = output.read_bytes()
    stock_data = stock.read_bytes()
    compiled = image.read_bytes()
    # Parse and compare the actual output slices independently of replace_kernel:
    # a bug that appended a full old boot must not validate itself here.
    info = layout(stock_data)
    header = bytearray(stock_data[:4096])
    struct.pack_into('<I', header, 8, len(compiled))
    new_end = 4096 + len(compiled)
    tail = stock_data[4096 + info['kernel_size']:info['original_image_size']]
    payload_end = new_end + len(tail)
    if (data[:4096] != header or data[4096:new_end] != compiled
            or data[new_end:payload_end] != tail):
        raise ValueError('Final boot header, compiled kernel or Samsung tail does not match the inputs')
    old_marker = stock_data.find(b'SEANDROIDENFORCE', info['kernel_end'], info['original_image_size'])
    marker = data.find(b'SEANDROIDENFORCE', new_end, payload_end)
    if old_marker >= 0 and (marker != new_end + old_marker - info['kernel_end']
                            or marker % 4096 != old_marker % 4096):
        raise ValueError('Final boot Samsung marker offset or alignment is incorrect')
    info.update({'new_kernel_size': len(compiled), 'new_kernel_offset': 4096,
                 'new_kernel_end': new_end, 'unsigned_image_size': payload_end,
                 'tail_size': len(tail), 'tail_sha256': hashlib.sha256(tail).hexdigest(),
                 'original_seandroid_marker_offset': old_marker, 'seandroid_marker_offset': marker,
                 'seandroid_4k_alignment_preserved': marker < 0 or marker % 4096 == old_marker % 4096})
    if len(data) != info['partition_size'] or len(data) % 4096:
        raise ValueError('Final boot file size must match the original boot and be 4 KiB aligned')
    if signed:
        final = layout(data)
        if len(data) != info['partition_size'] or not final['has_avb_footer']:
            raise ValueError('Signed boot partition size or AVB footer is incorrect')
        if final['original_image_size'] != payload_end:
            raise ValueError('Signed boot AVB original_image_size is incorrect')
    elif any(data[payload_end:]):
        raise ValueError('Unsigned output may contain only zero padding after the preserved Samsung tail; no old AVB metadata')
    certificates = verify_preserved_module_certificates(
        stock_data[4096:4096 + info['kernel_size']], data[4096:4096 + info['new_kernel_size']])
    return {'header_verified': True, 'kernel_payload_verified': True, 'samsung_tail_verified': True,
            'module_certificates': certificates, 'kernel_offset': 4096,
            'file_size_4k_aligned': len(data) % 4096 == 0,
            'same_size_as_original_boot': len(data) == len(stock_data),
            'payload_end': payload_end,
            'unsigned_zero_padding_bytes': None if signed else len(data) - payload_end,
            'avb_footer_present': data[-64:-60] == b'AVBf',
            'signed': signed, 'sha256': sha256(output), 'bytes': len(data), 'layout': info}


def package_boot(stock: Path, image: Path, output: Path, manifest: Path,
                 *, unsigned: bool, key_b64: str = '', salt: str | None = None) -> dict:
    rebuilt, info = replace_kernel(stock.read_bytes(), image.read_bytes())
    if output.exists():
        raise ValueError('boot output already exists; use a new output path')
    output.parent.mkdir(parents=True, exist_ok=True)
    avbtool = Path(__file__).with_name('avbtool.py')
    # Keep key material outside source/build/dist trees and expose it only after
    # compilation. Neither source Makefiles nor uploaded artifacts receive it.
    with tempfile.TemporaryDirectory(prefix='gw-boot-sign-') as temp_name:
        temp = Path(temp_name)
        candidate = temp / 'boot.img'
        candidate.write_bytes(rebuilt)
        key = temp / 'avb-key.pem'
        if not unsigned:
            decode_signing_key(key_b64, key)
            selected_salt = salt or sha256(image)
            if not re.fullmatch(r'[0-9a-fA-F]+', selected_salt) or len(selected_salt) % 2:
                raise ValueError('AVB salt must be an even-length hexadecimal string')
            subprocess.run(['python3', str(avbtool), 'add_hash_footer', '--image', str(candidate),
                            '--partition_size', str(info['partition_size']), '--partition_name', 'boot',
                            '--algorithm', 'SHA256_RSA4096', '--key', str(key), '--salt', selected_salt], check=True)
            subprocess.run(['python3', str(avbtool), 'verify_image', '--image', str(candidate),
                            '--key', str(key)], check=True)
        else:
            # Keep the original partition length and 4 KiB file alignment,
            # without copying the old AVB block/footer or creating a new one.
            with candidate.open('ab') as stream:
                stream.write(bytes(info['partition_size'] - len(rebuilt)))
        verified = verify_packaged(candidate, stock, image, signed=not unsigned)
        shutil.copyfile(candidate, output)
    report = {'output': output.name, 'stock_boot_sha256': sha256(stock), 'kernel_sha256': sha256(image),
              'avb_algorithm': None if unsigned else 'SHA256_RSA4096',
              'avb_salt': None if unsigned else selected_salt, 'verification': verified}
    write_json(manifest, report)
    print(f'Created {output.name}: {verified["bytes"]} bytes; SHA-256: {verified["sha256"]}')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    inspect = sub.add_parser('inspect')
    inspect.add_argument('--url')
    inspect.add_argument('--boot', type=Path)
    inspect.add_argument('--work', type=Path, required=True)
    pack = sub.add_parser('pack')
    pack.add_argument('--stock', type=Path, required=True)
    pack.add_argument('--kernel', type=Path, required=True)
    pack.add_argument('--output', type=Path, required=True)
    pack.add_argument('--manifest', type=Path, required=True)
    pack.add_argument('--unsigned', action='store_true')
    pack.add_argument('--salt')
    args = parser.parse_args()
    if args.command == 'inspect':
        if not args.url and not args.boot:
            parser.error('inspect requires --url or --boot')
        inspect_boot(args.boot or Path('download'), args.work, url=args.url)
    else:
        package_boot(args.stock, args.kernel, args.output, args.manifest,
                     unsigned=args.unsigned, key_b64=os.environ.get('AVB_PRIVATE_KEY_B64', ''), salt=args.salt)


if __name__ == '__main__':
    main()
