import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from find_source_links import (SamsungSearchParser, discover_rows, main, normalize_item,
                               read_dependencies, resolve, select_packages, summary,
                               validate_inputs)


BASE = 'SM-L305U_Opensource.zip'
DEVICE = 'SM-L315F_EUR_Opensource_dts.zip'
UPDATE = ('SM-L305U_Opensource_L305USQS3BZF4_L300XXS2BZF4_L300ZCS2BZF4_'
          'L305FXXS3BZF4_L305NKOS3BZF4_L310XXS2BZF4_L310ZCS2BZF4_'
          'L3150ZCS3BZF4_L315FXXS3BZF4_L315NKOS3BZF4_L315USQS3BZF4.zip')
OLDER = UPDATE.replace('BZF4', 'BZE1')
ROW = {'upload_id': '14120', 'models': ['SM-L305U', 'SM-L315F'],
       'versions': ['L305USQU3BZB4', 'L315FXXU3BZB4'],
       'filenames': [BASE, DEVICE, OLDER, UPDATE],
       'search_url': 'https://opensource.samsung.com/uploadSearch?searchValue=L315FXXS3BZF4'}


def metadata(*names):
    return {'metadata': {'identifier': 'samsung-opensource-14120'},
            'files': [{'name': name, 'source': 'original', 'size': '495466'} for name in names]}


def dependency(_):
    return {'readmes': [{'name': 'kernel.txt', 'base_versions': ['L305USQU3BZB4']}],
            'base_versions': ['L305USQU3BZB4']}


class SourceLinkTests(unittest.TestCase):
    def setUp(self):
        self.catalog = metadata(BASE, DEVICE, 'SM-L300_Opensource_dts.zip', OLDER, UPDATE)

    def select(self, firmware='L315FXXS3BZF4', row=ROW, catalog=None):
        return select_packages('SM-L315F', firmware, catalog or self.catalog, row, dependency)

    def test_watch7_expected_links_and_dependency_order(self):
        result = self.select()
        prefix = 'https://archive.org/download/samsung-opensource-14120/'
        self.assertEqual(result['source_urls'], ';'.join(prefix + name for name in (BASE, DEVICE, UPDATE)))
        self.assertEqual([p['role'] for p in result['packages']], ['base', 'device', 'update'])
        self.assertEqual(result['base_versions'], ['L305USQU3BZB4'])

    def test_previous_build_does_not_select_newer_patch(self):
        result = self.select('L315FXXS3BZE1')
        self.assertEqual([p['name'] for p in result['packages']], [BASE, DEVICE, OLDER])

    def test_base_build_needs_no_update(self):
        result = self.select('L315FXXU3BZB4')
        self.assertEqual([p['name'] for p in result['packages']], [BASE, DEVICE])

    def test_complete_firmware_token_required(self):
        with self.assertRaisesRegex(ValueError, '精确匹配'):
            self.select('L315FXXS3BZF', row=None)

    def test_unknown_build_does_not_fall_back_to_latest(self):
        with self.assertRaisesRegex(ValueError, '精确匹配'):
            self.select('L315FXXS3BZZ9')

    def test_explicit_item_matches_update_without_samsung(self):
        result = self.select(row=None)
        self.assertEqual(result['packages'][-1]['name'], UPDATE)
        with self.assertRaisesRegex(ValueError, '精确匹配'):
            self.select('L315FXXU3BZB4', row=None)

    def test_combined_model_dts_alias(self):
        name = 'SM-L315U_L3150_Opensource_dts.zip'
        for model, version in [('SM-L315U', 'L315USQS3BZF4'), ('SM-L3150', 'L3150ZCS3BZF4')]:
            result = select_packages(model, version, metadata(BASE, name, UPDATE), None, dependency)
            self.assertEqual(result['packages'][1]['name'], name)

    def test_missing_or_private_target_dts_is_rejected(self):
        for catalog in (metadata(BASE, UPDATE), metadata(BASE, DEVICE, UPDATE)):
            if len(catalog['files']) == 3:
                catalog['files'][1]['private'] = 'true'
            with self.assertRaisesRegex(ValueError, '缺少目标型号'):
                self.select(catalog=catalog)

    def test_overlay_alone_is_not_a_complete_package(self):
        with self.assertRaisesRegex(ValueError, '完整基础包'):
            self.select(catalog=metadata(DEVICE, UPDATE))

    def test_multiple_firmware_matches_fail(self):
        with self.assertRaisesRegex(ValueError, '多个候选'):
            self.select(catalog=metadata(BASE, DEVICE, UPDATE, 'SM-L315F_Opensource_L315FXXS3BZF4_diff.zip'))

    def test_readme_dependency_can_select_between_base_packages(self):
        result = self.select(catalog=metadata(BASE, 'SM-L700_Opensource.zip', DEVICE, UPDATE))
        self.assertEqual(result['packages'][0]['name'], BASE)

    def test_inconsistent_base_dependencies_fail(self):
        def inconsistent(entry):
            return {'readmes': [], 'base_versions': ['L305USQU3BZB4' if entry['name'] == DEVICE else 'L305USQU1AXFA']}
        with self.assertRaisesRegex(ValueError, '不同的基础版本'):
            select_packages('SM-L315F', 'L315FXXS3BZF4', self.catalog, ROW, inconsistent)

    def test_readme_base_must_belong_to_official_row(self):
        with self.assertRaisesRegex(ValueError, '不在该三星条目'):
            self.select(row=ROW | {'versions': []})

    def test_samsung_search_ignores_mobile_duplicates_and_other_models(self):
        document = '''<table><tr>
          <td class="col-model" title="SM-L305U&#10;SM-L315F">SM-L305U<br>SM-L315F</td>
          <td class="col-version" title="L305USQU3BZB4&#10;L315FXXU3BZB4">L305USQU3BZB4</td>
          <td class="col-desc" title="''' + UPDATE + '''">''' + DEVICE + '''</td>
          <td><a href="javascript:showSrcDownPop('14120');">Download</a></td></tr>
          <tr class="m-column"><td><a href="javascript:showSrcDownPop('14120');">Download</a></td></tr>
          <tr><td class="col-model">SM-L315FN</td><td class="col-desc">''' + UPDATE + '''</td>
          <td><a href="javascript:showSrcDownPop('999');">Download</a></td></tr></table>'''
        parser = SamsungSearchParser()
        parser.feed(document)
        self.assertEqual(len(parser.rows), 2)
        class FakeClient:
            def read(self, _):
                return document.encode()
        rows, errors = discover_rows(FakeClient(), 'SM-L315F', 'L315FXXS3BZF4')
        self.assertEqual([r['upload_id'] for r in rows], ['14120'])
        self.assertEqual(errors, [])

    def test_unreadable_candidate_is_not_silently_ignored(self):
        class FakeClient:
            def metadata(self, _):
                raise RuntimeError('HTTP 503')
        report = {}
        with self.assertRaisesRegex(ValueError, '未能唯一解析'):
            resolve(FakeClient(), 'SM-L315F', 'L315FXXS3BZF4', '14120', report)
        self.assertEqual(report['candidate_errors'][0]['archive_item'], 'samsung-opensource-14120')

    def test_small_overlay_zip_readme_and_hash_checked(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z:
            z.writestr('L315FXXU3BZB4_kernel.txt', '1. Download and unzip the kernel source of L305USQU3BZB4.\n')
            z.writestr('kernel.tar.gz', b'not executed or unpacked')
        data = buffer.getvalue()
        class FakeClient:
            def read(self, url, limit):
                return data
        entry = {'name': DEVICE, 'size': str(len(data)), 'sha1': hashlib.sha1(data).hexdigest()}
        result = read_dependencies(FakeClient(), 'samsung-opensource-14120', entry)
        self.assertEqual(result['base_versions'], ['L305USQU3BZB4'])
        with self.assertRaisesRegex(ValueError, '校验值'):
            read_dependencies(FakeClient(), 'samsung-opensource-14120', entry | {'sha1': '0' * 40})

    def test_inputs_and_item_override(self):
        self.assertEqual(validate_inputs(' sm-l315f ', ' l315fxxs3bzf4 '), ('SM-L315F', 'L315FXXS3BZF4'))
        for value in ('14120', 'samsung-opensource-14120', 'https://archive.org/details/samsung-opensource-14120'):
            self.assertEqual(normalize_item(value), 'samsung-opensource-14120')
        for model, firmware in [('SM-L315F\n::error::oops', 'L315FXXS3BZF4'), ('SM-L315F', 'L305USQS3BZF4')]:
            with self.assertRaises(ValueError):
                validate_inputs(model, firmware)
        with self.assertRaises(ValueError):
            normalize_item('https://example.com/details/samsung-opensource-14120')

    def test_summary_has_copyable_urls_and_readme_evidence(self):
        result = self.select()
        rendered = summary({'model': 'SM-L315F', 'firmware': 'L315FXXS3BZF4', 'status': 'success',
                            'result': result, 'discovery': {'rows': [ROW]}})
        self.assertIn('```text\n' + result['source_urls'] + '\n```', rendered)
        self.assertIn('L305USQU3BZB4', rendered)
        self.assertIn('完整基础包', rendered)

    def test_failure_writes_summary_and_removes_stale_urls(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / 'out'
            out.mkdir()
            (out / 'source-urls.txt').write_text('old URLs')
            step_summary = Path(folder) / 'github-summary.md'
            with patch.object(sys, 'argv', ['find_source_links.py', '--model', 'bad', '--firmware', 'bad', '--output', str(out)]), \
                    patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(step_summary)}):
                self.assertEqual(main(), 1)
            self.assertFalse((out / 'source-urls.txt').exists())
            self.assertEqual(json.loads((out / 'source-links.json').read_text(encoding='utf-8'))['status'], 'failed')
            self.assertIn('解析失败', step_summary.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
