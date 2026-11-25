import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from prepare_kernel import build_settings, install_bakasu, repository_url
from compile_kernel import check_config


class PrepareTests(unittest.TestCase):
    def test_settings_follow_stock_release_and_source_soc_without_forcing_screen_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'src/kernel'
            source.mkdir(parents=True)
            (source / 'Makefile').write_text('VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = 150\nEXTRAVERSION =\n')
            (source / 'build.config.constants').write_text('BRANCH=android14-6.1\nCLANG_VERSION=r487747c\n')
            for name in ('erd5535', 's5e5535'):
                project = source.parent / 'projects' / name
                project.mkdir(parents=True)
                (project / f'build.config.{name}.common').write_text('TARGET_SOC=s5e5535\n')
            config = root / 'stock.config'
            config.write_text('CONFIG_LOCALVERSION="-regional"\n')
            manifest = root / 'sources.json'
            manifest.write_text(json.dumps({'packages': []}))
            settings = build_settings(source, {'kernel_release': '6.1.150-regional-android14-12'}, config, manifest)
            self.assertEqual(settings['release_suffix'], '-android14-12')
            self.assertEqual(settings['release_prepend'], '')
            self.assertFalse((source / '.scmversion').exists())
            self.assertEqual(settings['target_soc'], 's5e5535')
            self.assertNotIn('target_product', settings)

    def test_config_check_requires_bakasu_and_preserves_module_and_compat_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stock, current = root / 'stock', root / 'current'
            stock.write_text('CONFIG_COMPAT=y\nCONFIG_MODULE_SIG_PROTECT=y\n')
            current.write_text(stock.read_text() + 'CONFIG_KSU=y\n')
            self.assertTrue(check_config(stock, current, {'KSU': 'y'})['passed'])
            current.write_text('CONFIG_KSU=y\n# CONFIG_COMPAT is not set\nCONFIG_MODULE_SIG_PROTECT=y\n')
            self.assertFalse(check_config(stock, current, {'KSU': 'y'})['passed'])

    def test_repository_normalization_and_argument_rejection(self):
        self.assertEqual(repository_url(''), 'https://github.com/Baka-SU/BakaSU.git')
        self.assertEqual(repository_url('someone/fork'), 'https://github.com/someone/fork')
        for value in ('--upload-pack=command', 'ssh://host/repo', 'https://host/repo;command', 'https://user:pass@host/repo'):
            with self.assertRaises(ValueError):
                repository_url(value)

    def test_default_branch_and_explicit_commit_resolution_keep_git_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            origin = root / 'origin'
            subprocess.run(['git', 'init', '-q', '-b', 'custom-default', str(origin)], check=True)
            (origin / 'kernel').mkdir()
            (origin / 'kernel/Kconfig').write_text('config KSU\n')
            env = os.environ.copy()
            env.update(GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                       GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
            commits = []
            for value in ('first', 'second'):
                (origin / 'value').write_text(value)
                subprocess.run(['git', '-C', str(origin), 'add', '.'], env=env, check=True)
                subprocess.run(['git', '-C', str(origin), 'commit', '-q', '-m', value], env=env, check=True)
                commits.append(subprocess.check_output(['git', '-C', str(origin), 'rev-parse', 'HEAD'], text=True).strip())
            # Use a local Git server fixture while retaining the production URL validator.
            with patch('prepare_kernel.repository_url', return_value=str(origin)):
                for name, revision, expected in [('latest', '', commits[1]), ('pinned', commits[0], commits[0])]:
                    source = root / name
                    source.mkdir()
                    info = install_bakasu(source, 'unused', revision)
                    self.assertEqual(info['commit'], expected)
                    self.assertEqual(info['default_branch'], 'refs/remotes/origin/custom-default')
                    shallow = subprocess.check_output(['git', '-C', str(source / 'BakaSU'),
                                                       'rev-parse', '--is-shallow-repository'], text=True).strip()
                    self.assertEqual(shallow, 'false')


if __name__ == '__main__':
    unittest.main()
