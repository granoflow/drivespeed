"""Synthetic evidence only; no model, device collection or external requests."""
import copy
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from product_profile import canonical_url, load_profile, validate_profile, matches_profile
from report import render_report
import report
import hardware
from progress import messages
from test_community import local_result
from community_data import project_result, transport


def fixture_profile():
    source = dict(kind='review', publisher='Fixture Publisher', title='Fixture SSD 2TB review',
                  display_name='Fixture SSD review', url='https://reviews.example.com/Drive?id=2',
                  canonical_url='https://reviews.example.com/Drive?id=2', url_aliases=[],
                  identity_basis='normalized_url', name_method='title_fallback', model=None, provider=None,
                  checked_on='2026-10-30', scope='2TB fixture, not a real product', availability='verified')
    return dict(schema_version=1, product_id='fixture-ssd-2tb', raw_model='Fixture SSD 2TB', raw_model_aliases=[],
                capacity_bytes=2_000_000_000_000, official_name=None, display_name='Fixture SSD 2TB',
                name_basis='review', revision=0, official_sources=[], review_sources=[source])


class ProductProfileTests(unittest.TestCase):
    def test_shared_query_key_vectors_match_profile_consumer(self):
        vectors = json.loads((Path(__file__).parent / 'fixtures/product-url-cases.json').read_text())
        for vector in vectors:
            with self.subTest(vector=vector['id']):
                profile = fixture_profile()
                profile['review_sources'][0].update(url=vector['url'], canonical_url=vector['url'])
                if vector['canonical'] is None:
                    with self.assertRaisesRegex(ValueError, 'invalid_profile'):
                        canonical_url(vector['url'])
                    with self.assertRaisesRegex(ValueError, 'invalid_profile'):
                        validate_profile(profile)
                else:
                    self.assertEqual(canonical_url(vector['url']), vector['canonical'])
                    validate_profile(profile)

    def test_url_identity_is_conservative_and_offline(self):
        self.assertEqual(canonical_url('https://Reviews.Example.com:443/Drive?id=2&utm_source=x&id=3#section'),
                         'https://reviews.example.com/Drive?id=2&id=3')
        self.assertNotEqual(canonical_url('https://example.com/#/one'), canonical_url('https://example.com/#/two'))
        for url in ['http://example.com/', 'https://127.0.0.1/', 'https://user:pass@example.com/',
                    'https://example.local/', 'https://example.com:8443/']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                canonical_url(url)

    def test_strict_input_rejects_duplicate_fields_and_semantic_conflicts(self):
        p = fixture_profile()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.json'
            source.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaises(ValueError):
                load_profile(source)
            source.write_text(json.dumps(p))
            self.assertEqual(load_profile(source), p)
        for mutate in [lambda p: p.update(capacity_bytes=1_000_000_000_000),
                       lambda p: p['review_sources'][0].update(model='pretend-model'),
                       lambda p: p['review_sources'][0].update(checked_on='2026-02-30'),
                       lambda p: p.update(extra='unexpected')]:
            bad = copy.deepcopy(p); mutate(bad)
            with self.assertRaises(ValueError):
                validate_profile(bad)

    def test_nominal_identity_never_uses_filesystem_capacity(self):
        p = fixture_profile()
        result = {'target': {'scope': 'physical', 'physical_devices': ['fixture-disk']},
                  'hardware': {'drive': {'model': 'Fixture SSD 2TB'}}}
        self.assertTrue(matches_profile(p, result))  # Historical model-only metadata.
        result['hardware']['drive']['model'] = 'Fixture SSD 1TB'
        self.assertFalse(matches_profile(p, result))
        p['raw_model'] = 'Unknown capacity SSD'
        result['hardware']['drive']['model'] = p['raw_model']
        result['target']['total_bytes'] = p['capacity_bytes']
        self.assertFalse(matches_profile(p, result))

    def test_multi_disk_display_name_cannot_become_single_drive_evidence(self):
        nodes = [{'bsd_name': name, 'device_model': 'Fixture SSD 2TB'} for name in ['disk8', 'disk9']]
        for observed in [nodes, nodes[:1]]:  # Same models, or only one model exposed.
            facts = hardware.mac_hardware({'SPNVMeDataType': observed}, ['disk8', 'disk9'])
            self.assertEqual(facts['drive']['model'], 'Fixture SSD 2TB')
            self.assertIsNone(facts['drive']['matching_model'])
            result = {'target': {'scope': 'multi_device', 'physical_devices': ['disk8', 'disk9']}, 'hardware': facts}
            self.assertFalse(matches_profile(fixture_profile(), result))
            result['target']['scope'] = 'physical'  # Inconsistent metadata cannot override the count.
            self.assertFalse(matches_profile(fixture_profile(), result))
        for scope in ['unresolved', 'virtual', 'multi_device']:
            result = {'target': {'scope': scope, 'physical_devices': ['disk8']},
                      'hardware': {'drive': {'model': 'Fixture SSD 2TB', 'matching_model': 'Fixture SSD 2TB'}}}
            self.assertFalse(matches_profile(fixture_profile(), result))
        result['target']['scope'] = 'physical'
        self.assertTrue(matches_profile(fixture_profile(), result))
        result['hardware']['drive']['matching_model'] = None
        self.assertFalse(matches_profile(fixture_profile(), result))

    def test_sidecar_export_preserves_raw_and_prior_output_on_identity_error(self):
        result = local_result(); result.pop('user_input', None)
        result['target']['physical_devices'] = ['fixture-disk']
        result['hardware']['drive']['model'] = 'Fixture SSD 2TB'
        result['hardware']['drive']['matching_model'] = 'Fixture SSD 2TB'
        before = json.dumps(result, sort_keys=True)
        original_upload = transport(project_result(result, 'https://x.com/fixture_user', submission_id='11111111-1111-4111-8111-111111111111'))
        with tempfile.TemporaryDirectory() as folder:
            paths = render_report(result, folder, 'en', messages('en'), product_profile=fixture_profile())
            self.assertEqual(json.loads(Path(paths['product_profile']).read_text())['product_id'], 'fixture-ssd-2tb')
            previous = Path(paths['html']).read_bytes()
            bad = fixture_profile(); bad['raw_model'] = 'Different SSD 2TB'
            with self.assertRaisesRegex(ValueError, 'identity_conflict'):
                render_report(result, folder, 'en', messages('en'), product_profile=bad)
            self.assertEqual(Path(paths['html']).read_bytes(), previous)
            sidecar = Path(paths['product_profile']); sidecar.unlink()
            sidecar.symlink_to(Path(paths['html']))
            with self.assertRaisesRegex(ValueError, 'output_failed'):
                render_report(result, folder, 'en', messages('en'), product_profile=fixture_profile())
            self.assertEqual(Path(paths['html']).read_bytes(), previous)
        self.assertEqual(json.dumps(result, sort_keys=True), before)
        self.assertEqual(transport(project_result(result, 'https://x.com/fixture_user', submission_id='11111111-1111-4111-8111-111111111111')), original_upload)

    def test_explicit_missing_sidecar_fails_cli_before_overwriting_existing_report(self):
        result = local_result(); result.pop('user_input', None)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'results.json'; source.write_text(json.dumps(result))
            old = Path(folder) / 'report.html'; old.write_text('previous artifact')
            args = ['report.py', '--input', str(source), '--output', folder, '--language', 'en',
                    '--product-profile', str(Path(folder) / 'missing.json')]
            with patch.object(sys, 'argv', args), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(report.main(), 2)
            self.assertEqual(old.read_text(), 'previous artifact')


class ProductPresentationTests(unittest.TestCase):
    def result(self):
        result = local_result(); result.pop('user_input', None)
        result['target']['physical_devices'] = ['fixture-disk']
        result['hardware']['drive'].update(model='Fixture SSD 2TB', matching_model='Fixture SSD 2TB')
        return result

    def test_name_sources_dates_and_raw_identity_are_rendered_without_changing_measurements(self):
        result = self.result(); before = json.dumps(result, sort_keys=True)
        profile = fixture_profile(); profile['display_name'] = 'Fixture Official NVMe SSD 2TB'
        review = copy.deepcopy(profile['review_sources'][0]); review.update(url='https://example.com/review-two', canonical_url='https://example.com/review-two',
            display_name='A long <script>not executable</script> review title', checked_on='2026-10-28')
        profile['review_sources'].append(review)
        for lang in ['en', 'zh']:
            with self.subTest(language=lang), tempfile.TemporaryDirectory() as folder:
                paths = render_report(result, folder, lang, messages(lang), product_profile=profile)
                html = Path(paths['html']).read_text()
                self.assertIn('Fixture Official NVMe SSD 2TB', html)
                self.assertIn('<td>Fixture Official NVMe SSD 2TB</td><td class="muted">'+messages(lang)['product_resources']+'</td>', html)
                self.assertIn('<td>Fixture SSD 2TB</td><td>'+messages(lang)['source_system']+'</td>', html)
                self.assertIn(messages(lang)['original_model'], html)
                self.assertIn('Fixture SSD 2TB', html)
                self.assertIn('id="product-resources"', html)
                self.assertNotIn(messages(lang)['official_resources']+'</h3>', html)
                self.assertIn('2026-10-28', html); self.assertIn('2026-10-30', html)
                self.assertIn('&lt;script&gt;not executable&lt;/script&gt;', html)
                self.assertNotIn('<script>not executable</script>', html)
                self.assertIn('2TB fixture, not a real product', html)
                self.assertEqual(html.count('class="product-source-group"'), 1)
        self.assertEqual(json.dumps(result, sort_keys=True), before)

    def test_reliable_name_survives_empty_or_unavailable_sources_without_placeholders(self):
        profile = fixture_profile(); profile['display_name'] = 'Fixture Named Product 2TB'
        profile['review_sources'][0]['availability'] = 'unavailable'
        with tempfile.TemporaryDirectory() as folder:
            artifacts = render_report(self.result(), folder, 'en', messages('en'), product_profile=profile)
            html = Path(artifacts['html']).read_text()
            self.assertIn('Fixture Named Product 2TB', html)
            self.assertNotIn('id="product-resources"', html)

    def test_product_name_does_not_inherit_user_supplied_origin(self):
        result = self.result()
        result['hardware']['drive']['source'] = 'user'
        profile = fixture_profile()
        profile['display_name'] = 'Fixture Named Product 2TB'
        with tempfile.TemporaryDirectory() as folder:
            html = Path(render_report(result, folder, 'en', messages('en'), product_profile=profile)['html']).read_text()
            self.assertIn('<td>Fixture Named Product 2TB</td><td class="muted">Device resources</td>', html)
            self.assertIn('<td>Fixture SSD 2TB</td><td>User supplied</td>', html)

    def test_target_bound_interface_is_system_evidence_and_arbitrary_sources_are_rejected(self):
        result = self.result()
        result['hardware']['access_interface'] = {'name':'USB4 40 Gbps', 'source':'ioreg:target-ancestor'}
        result['hardware']['controller'] = {'model':'ASMedia 246x','source':'ioreg:target-ancestor','precision':'series'}
        before = copy.deepcopy(result)
        with tempfile.TemporaryDirectory() as folder:
            html = Path(render_report(result, folder, 'en', messages('en'))['html']).read_text()
            self.assertIn('USB4 40 Gbps', html); self.assertIn('ASMedia 246x', html)
            self.assertIn('system identifier', html); self.assertNotIn('ASM2464PD', html)
            self.assertEqual(result, before)
            result['hardware']['access_interface']['source'] = 'some-unbound-candidate'
            with self.assertRaisesRegex(ValueError, 'Invalid access interface'):
                render_report(result, folder, 'en', messages('en'))
