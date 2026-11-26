"""Shared GW7/GW8 kernel release reconstruction, derived only from build inputs.

There are no pinned firmware, Android, kernel or page-size versions here.
Kernel builds place output localversion* files before source localversion*
files and CONFIG_LOCALVERSION, followed by the LOCALVERSION environment value.
"""
from __future__ import annotations

import re
from pathlib import Path


STAMP_FILE = 'localversion.gw-boot'


def localversion_residual(version: str, expected: str, observed: str) -> str:
    """Return the extra LOCALVERSION needed after an observed probe.

    The probe is run with an empty LOCALVERSION and no inherited Git tree.
    This lets the source's own ``setlocalversion`` implementation decide
    whether it already supplies Android/KMI or firmware fragments. We only
    append a residual fragment when the observed string is a prefix of the
    requested release; no firmware/version token is hard-coded here.
    """
    if not expected.startswith(version) or not observed.startswith(version):
        raise ValueError(f'Kernel release base differs: expected={expected!r}, observed={observed!r}')
    expected_suffix = expected[len(version):]
    observed_suffix = observed[len(version):]
    if not expected_suffix.startswith(observed_suffix):
        raise ValueError('Kernel release probe contains a fragment in the wrong order: '
                         f'expected={expected!r}, observed={observed!r}')
    return expected_suffix[len(observed_suffix):]


def release_settings(source: Path, version: str, release: str, config: dict[str, str]) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9_.+\-]+', release):
        raise ValueError('Invalid original kernel release string')
    if not release.startswith(version) or (len(release) > len(version) and release[len(version)].isdigit()):
        raise ValueError(f'Source kernel {version} does not match original boot kernel {release}; use matching sources')
    source_version = ''.join(p.read_text().rstrip('\n') for p in sorted(source.glob('localversion*'))
                             if p.is_file() and '~' not in p.name)
    config_version = config.get('CONFIG_LOCALVERSION', '""').strip('"')
    fixed = source_version + config_version
    if fixed and not re.fullmatch(r'[A-Za-z0-9_.+\-]+', fixed):
        raise ValueError('Invalid source/config local version string')
    # Historical GW7 layouts: version + config suffix + Android/build suffix.
    if release.startswith(version + fixed):
        prepend, suffix = '', release[len(version + fixed):]
    # Samsung Kleaf layouts: version + Android/build stamp + config suffix.
    # E.g. CONFIG_LOCALVERSION contains a page-size suffix at the very end.
    elif fixed and release.endswith(fixed):
        prepend, suffix = release[len(version):-len(fixed)], ''
    else:
        raise ValueError('Original boot release cannot be reconstructed while preserving '
                         f'source/config local version {fixed!r}: {release}')
    return {'kernel_release': release, 'kernel_source_version': version,
            'release_suffix': suffix, 'release_prepend': prepend,
            'source_localversion': source_version, 'config_localversion': config_version}


def configure_release_files(source: Path, output: Path, settings: dict,
                            *, include_prepend: bool = True) -> None:
    """Write only build stamp metadata; preserve the recovered kernel config."""
    output.mkdir(parents=True, exist_ok=True)
    stamp = output / STAMP_FILE
    prepend = settings.get('release_prepend', '') if include_prepend else ''
    if prepend:
        stamp.write_text(prepend + '\n')
    else:
        stamp.unlink(missing_ok=True)
    # Older kernels may have a cached SCM suffix. Reconstruct it from the
    # original boot rather than adding it a second time after LOCALVERSION.
    cached = source / '.scmversion'
    if cached.is_file():
        cached.write_text('')
