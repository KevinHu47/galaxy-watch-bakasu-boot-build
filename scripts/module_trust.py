#!/usr/bin/env python3
"""Verify recovered public module keys in the kernel's actual built-in trust list."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from elftools.elf.elffile import ELFFile

from common import write_json


def parse_certificates(blob: bytes) -> list[bytes]:
    """Parse the concatenated DER objects between __cert_list_start/end."""
    certificates, offset = [], 0
    while offset < len(blob):
        if blob[offset:offset + 1] != b'\x30' or len(blob) - offset < 2:
            raise ValueError('Invalid built-in kernel X.509 certificate list')
        length, header = blob[offset + 1], 2
        if length & 0x80:
            count = length & 0x7f
            if not 1 <= count <= 4 or len(blob) - offset < 2 + count:
                raise ValueError('Invalid certificate DER length')
            length = int.from_bytes(blob[offset + 2:offset + 2 + count], 'big')
            header += count
        end = offset + header + length
        if end > len(blob):
            raise ValueError('Truncated built-in kernel certificate')
        der = blob[offset:end]
        x509.load_der_x509_certificate(der)
        certificates.append(der)
        offset = end
    return certificates


def builtin_certificates(vmlinux: Path) -> list[bytes]:
    """Use ELF trust-list symbols so arbitrary embedded DER bytes cannot pass."""
    with vmlinux.open('rb') as stream:
        elf = ELFFile(stream)
        symtab = elf.get_section_by_name('.symtab')
        if symtab is None:
            raise ValueError('vmlinux is missing its symbol table for module trust verification')
        starts = symtab.get_symbol_by_name('__cert_list_start')
        ends = symtab.get_symbol_by_name('__cert_list_end')
        if not starts or not ends:
            raise ValueError('vmlinux is missing the built-in module certificate list symbols')
        start, end = starts[0], ends[0]
        if not isinstance(start['st_shndx'], int):
            raise ValueError('Invalid built-in certificate section')
        section = elf.get_section(start['st_shndx'])
        offset = start['st_value'] - section['sh_addr']
        length = end['st_value'] - start['st_value']
        if offset < 0 or length < 0 or offset + length > section['sh_size']:
            raise ValueError('Invalid built-in module certificate list bounds')
        blob = section.data()[offset:offset + length]
    return parse_certificates(blob)


def verify_module_trust(vmlinux: Path, image: Path, recovered_keys: Path, report: Path) -> dict:
    pem = recovered_keys.read_bytes()
    expected = [cert.public_bytes(serialization.Encoding.DER)
                for cert in x509.load_pem_x509_certificates(pem)] if pem else []
    actual = builtin_certificates(vmlinux) if expected else []
    payload = image.read_bytes()
    actual_hashes = [hashlib.sha256(der).hexdigest() for der in actual]
    rows = []
    for der in expected:
        fingerprint = hashlib.sha256(der).hexdigest()
        rows.append({'sha256_der': fingerprint, 'in_builtin_trust_list': der in actual,
                     'in_compiled_Image': der in payload})
    result = {'passed': all(row['in_builtin_trust_list'] and row['in_compiled_Image'] for row in rows),
              'original_module_certificates': rows, 'builtin_certificate_sha256': actual_hashes,
              'scope': 'Original public module keys in actual vmlinux trust list and compiled Image; independent of boot AVB signing.'}
    write_json(report, result)
    if not result['passed']:
        raise ValueError('An original module signing certificate is absent from the built-in trust list or Image; see module-trust.json')
    print(f'Original module trust verified: {len(rows)} certificate(s) in kernel keyring and Image', flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vmlinux', type=Path, required=True)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--certificate', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    verify_module_trust(args.vmlinux, args.image, args.certificate, args.report)
