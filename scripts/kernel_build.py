"""Shared kernel configuration validation helpers."""
from __future__ import annotations

from pathlib import Path

from kernel_prepare import read_config


def check_config(stock: Path, current: Path, overrides: dict) -> dict:
    before, after = read_config(stock), read_config(current)
    changes = {k: {'stock': before.get(k, 'n'), 'build': after.get(k, 'n')}
               for k in sorted(before.keys() | after.keys()) if before.get(k, 'n') != after.get(k, 'n')}
    missing = {}
    for name, value in overrides.items():
        expected = value if value in ('y', 'n') else '"' + value + '"'
        if after.get('CONFIG_' + name, 'n') != expected:
            missing[name] = {'expected': expected, 'actual': after.get('CONFIG_' + name, 'n')}
    important = ['CONFIG_COMPAT', 'CONFIG_MODVERSIONS', 'CONFIG_MODULE_SIG_PROTECT',
                 'CONFIG_MODULE_SIG_FORCE', 'CONFIG_SECURITY_SELINUX', 'CONFIG_ARM64']
    preserved = {k: before.get(k, 'n') == after.get(k, 'n') for k in important}
    intentional = {'CONFIG_' + k for k in overrides}
    host = {'CONFIG_CC_VERSION_TEXT', 'CONFIG_GCC_VERSION', 'CONFIG_CLANG_VERSION',
            'CONFIG_AS_VERSION', 'CONFIG_LD_VERSION', 'CONFIG_PAHOLE_VERSION'}
    host_prefixes = ('CONFIG_CC_HAS_', 'CONFIG_AS_HAS_', 'CONFIG_LD_HAS_', 'CONFIG_PAHOLE_HAS_')
    unrelated = {k: v for k, v in changes.items()
                 if k not in intentional | host and not k.startswith(host_prefixes)
                 and not k.startswith('CONFIG_KSU')
                 and not (v['stock'] == 'n' and v['build'] == 'n')}
    return {'changes': changes, 'missing_required_settings': missing,
            'stock_settings_preserved': preserved, 'other_changes': unrelated,
            'passed': not missing and all(preserved.values())}
