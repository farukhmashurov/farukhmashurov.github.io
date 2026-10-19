"""Offline unit and regression tests against the actual uploaded homepage HTML."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from update_publications import (  # noqa: E402
    Crossref, Overrides, SiteFormatError, citation_html, expected_coauthors,
    journal_dois_in_li, match_record, normalized, update_html,
)

ORIGINAL = (ROOT / 'tests' / 'fixtures' / 'index_initial.html').read_text(encoding='utf-8')
SETTINGS = Overrides.load(ROOT / 'scripts' / 'publication_overrides.json')

FREE = {
    'DOI': '10.5555/journal.2026.11',
    'title': ['Free Novikov-Zinbiel algebra'],  # Official arXiv title is singular.
    'type': 'journal-article',
    'author': [
        {'family': 'Dauletiyarova', 'given': 'A.'},
        {'family': 'Mashurov', 'given': 'F.'},
        {'family': 'Sartayev', 'given': 'B.'},
    ],
    'container-title': ['Journal of Example Mathematics'],
    'published-online': {'date-parts': [[2026, 10, 10]]},
    'volume': '12',
    'issue': '2',
    'page': '120-145',
}
OPERADS = {
    'DOI': '10.5555/journal.2026.12',
    'title': ['Nonsymmetric versions of binary quadratic operads'],
    'type': 'journal-article',
    'author': [{'family': 'Mashurov', 'given': 'F.'}, {'family': 'Sartayev', 'given': 'B.'}],
    'container-title': ['Example Operads Quarterly'],
    'published-online': {'date-parts': [[2026, 10, 12]]},
}
BINARY = {
    'DOI': '10.5555/journal.2026.13',
    'title': ['Binary bicommutative and binary metabelian Lie algebras'],
    'type': 'journal-article',
    'author': [{'family': 'Mashurov', 'given': 'F.'}, {'family': 'Ismailov', 'given': 'N.'}],
    'container-title': ['Example Algebra Letters'],
    'published-online': {'date-parts': [[2026, 10, 12]]},
}


class OfflineCrossref(Crossref):
    """Mocks transport, but not Crossref's matching/ambiguity algorithms."""
    def __init__(self, records=()):
        super().__init__()
        self.records = [copy.deepcopy(r) for r in records]
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        if '/works?' in url:
            return {'message': {'items': self.records}}
        needle = url.split('/works/', 1)[-1].lower()
        for record in self.records:
            if record['DOI'].lower() == needle:
                return {'message': record}
        return None


class HomepageRegressionTests(unittest.TestCase):
    def test_source_is_exact_current_homepage_structure(self):
        self.assertIn('<section id="publications"', ORIGINAL)
        self.assertIn('<h3 id="preprints">', ORIGINAL)
        self.assertIn('<h3 id="peer-reviewed-journal-articles">', ORIGINAL)
        self.assertIn('href="style.css"', ORIGINAL)
        self.assertIn('src="script.js"', ORIGINAL)
        self.assertIn('src="profile.jpeg"', ORIGINAL)
        self.assertIn('Last updated: May 2026.', ORIGINAL)

    def test_current_site_still_has_expected_sections(self):
        # Unlike the historical fixture, the real index.html may change later
        # after a scheduled DOI promotion; this smoke test must keep working.
        current = (ROOT / 'index.html').read_text(encoding='utf-8')
        _, stats = update_html(current, OfflineCrossref(), SETTINGS, update_footer=False)
        self.assertGreaterEqual(stats.checked_preprints, 0)

    def test_configuration_contains_official_arxiv_title_variant(self):
        self.assertEqual(SETTINGS.aliases_by_arxiv['2608.27435'], ['Free Novikov-Zinbiel algebra'])
        self.assertEqual(normalized('Free Novikov–Zinbiel algebras'), 'free novikov zinbiel algebras')

    def test_parser_extracts_expected_coauthors(self):
        start = ORIGINAL.index('<strong>Free Novikov-Zinbiel algebras</strong>')
        end = ORIGINAL.index('</li>', start) + 5
        self.assertEqual(expected_coauthors(ORIGINAL[start:end]), {'dauletiyarova', 'sartayev'})

    def test_full_html_no_known_publication_is_idempotent(self):
        changed, stats = update_html(ORIGINAL, OfflineCrossref(), SETTINGS)
        self.assertEqual(ORIGINAL, changed)
        self.assertEqual(stats.checked_preprints, 4)
        self.assertEqual(stats.pending, 4)
        self.assertEqual(stats.promoted, 0)

    def test_promotes_to_journal_section_not_just_status(self):
        changed, stats = update_html(ORIGINAL, OfflineCrossref([FREE]), SETTINGS)
        self.assertEqual(stats.promoted, 1)
        before, after = changed.split('<h3 id="peer-reviewed-journal-articles">', 1)
        self.assertNotIn('<strong>Free Novikov-Zinbiel algebras</strong>', before)
        self.assertIn('<strong>Free Novikov-Zinbiel algebras</strong>', after)
        self.assertIn('<em>Journal of Example Mathematics</em>, <strong>12</strong>(2), 120-145, 2026.', after)
        self.assertIn('with A. Dauletiyarova, B. Sartayev.', after)
        self.assertIn('https://arxiv.org/abs/2608.27435', after)
        self.assertIn('https://doi.org/10.5555/journal.2026.11', after)
        self.assertEqual(changed.count('Free Novikov-Zinbiel algebras</strong>'), 1)
        self.assertIn('src="profile.jpeg"', changed)
        self.assertIn('src="script.js"', changed)
        self.assertIn('<h2>Academic path</h2>', changed)
        self.assertIn('Last updated: ', changed)
        self.assertNotIn('Last updated: May 2026.', changed)

    def test_preserves_other_page_sections_byte_for_byte(self):
        changed, _ = update_html(ORIGINAL, OfflineCrossref([FREE]), SETTINGS, update_footer=False)
        prefix = ORIGINAL.split('<section id="publications"', 1)[0]
        self.assertTrue(changed.startswith(prefix))
        self.assertEqual(changed.split('</section>\n\n      <section id="talks-and-conferences"', 1)[1],
                         ORIGINAL.split('</section>\n\n      <section id="talks-and-conferences"', 1)[1])

    def test_second_run_no_duplicates_and_no_unncessary_change(self):
        crossref = OfflineCrossref([FREE])
        changed, _ = update_html(ORIGINAL, crossref, SETTINGS)
        again, stats = update_html(changed, OfflineCrossref([FREE]), SETTINGS)
        self.assertEqual(changed, again)
        self.assertEqual(stats.promoted, 0)
        self.assertEqual(stats.refreshed, 0)

    def test_refreshes_only_auto_managed_entries(self):
        changed, _ = update_html(ORIGINAL, OfflineCrossref([FREE]), SETTINGS)
        amended = copy.deepcopy(FREE)
        amended['page'] = '120-150'
        refreshed, stats = update_html(changed, OfflineCrossref([amended]), SETTINGS)
        self.assertEqual(stats.refreshed, 1)
        self.assertIn('120-150, 2026.', refreshed)
        self.assertNotIn('120-145, 2026.', refreshed)
        self.assertIn('https://doi.org/10.1007/s40879-026-00892-7', refreshed)

    def test_refresh_with_initials_in_journal_name(self):
        abbreviated = copy.deepcopy(FREE)
        abbreviated['container-title'] = ['J. Algebra']
        first, _ = update_html(ORIGINAL, OfflineCrossref([abbreviated]), SETTINGS)
        revised = copy.deepcopy(abbreviated)
        revised['volume'] = '13'
        second, stats = update_html(first, OfflineCrossref([revised]), SETTINGS)
        self.assertEqual(stats.refreshed, 1)
        self.assertIn('<em>J. Algebra</em>, <strong>13</strong>', second)

    def test_multiple_promotions_keep_preprint_order(self):
        changed, stats = update_html(ORIGINAL, OfflineCrossref([FREE, OPERADS]), SETTINGS)
        self.assertEqual(stats.promoted, 2)
        _, journals = changed.split('<h3 id="peer-reviewed-journal-articles">', 1)
        self.assertLess(journals.index('Free Novikov-Zinbiel algebras'), journals.index('Nonsymmetric versions of binary quadratic operads'))
        self.assertLess(journals.index('Nonsymmetric versions of binary quadratic operads'), journals.index('On the transposed Poisson'))

    def test_preprint_without_arxiv_or_submitted_is_checked(self):
        changed, stats = update_html(ORIGINAL, OfflineCrossref([BINARY]), SETTINGS)
        self.assertEqual(stats.promoted, 1)
        before, after = changed.split('<h3 id="peer-reviewed-journal-articles">', 1)
        self.assertNotIn('Binary bicommutative and binary metabelian Lie algebras</strong>', before)
        self.assertIn('Binary bicommutative and binary metabelian Lie algebras</strong>', after)
        self.assertIn('https://doi.org/10.5555/journal.2026.13', after)

    def test_wrong_author_skips(self):
        wrong = copy.deepcopy(FREE)
        wrong['author'] = [{'family': 'SomeoneElse'}]
        changed, stats = update_html(ORIGINAL, OfflineCrossref([wrong]), SETTINGS)
        self.assertEqual(ORIGINAL, changed)
        self.assertEqual(stats.promoted, 0)

    def test_missing_coauthor_skips(self):
        wrong = copy.deepcopy(FREE)
        wrong['author'] = [{'family': 'Mashurov'}, {'family': 'Sartayev'}]
        changed, stats = update_html(ORIGINAL, OfflineCrossref([wrong]), SETTINGS)
        self.assertEqual(ORIGINAL, changed)
        self.assertEqual(stats.promoted, 0)

    def test_preprint_doi_does_not_count_as_journal(self):
        wrong = copy.deepcopy(FREE)
        wrong['DOI'] = '10.48550/arXiv.2608.27435'
        changed, stats = update_html(ORIGINAL, OfflineCrossref([wrong]), SETTINGS)
        self.assertEqual(ORIGINAL, changed)
        self.assertEqual(stats.promoted, 0)

    def test_journal_type_required(self):
        wrong = copy.deepcopy(FREE)
        wrong['type'] = 'posted-content'
        changed, stats = update_html(ORIGINAL, OfflineCrossref([wrong]), SETTINGS)
        self.assertEqual(ORIGINAL, changed)
        self.assertEqual(stats.promoted, 0)

    def test_two_matching_dois_require_review(self):
        alternate = copy.deepcopy(FREE)
        alternate['DOI'] = '10.5555/journal.2026.22'
        changed, stats = update_html(ORIGINAL, OfflineCrossref([FREE, alternate]), SETTINGS)
        self.assertEqual(ORIGINAL, changed)
        self.assertEqual(stats.skipped_ambiguous, 1)

    def test_bad_citation_metadata_is_html_escaped(self):
        bad = copy.deepcopy(FREE)
        bad['container-title'] = ['<img onerror=alert(1)>']
        self.assertNotIn('<img', citation_html(bad))

    def test_invalid_markup_aborts_not_rewrites(self):
        with self.assertRaises(SiteFormatError):
            update_html(ORIGINAL.replace('id="preprints"', 'id="elsewhere"'), OfflineCrossref([FREE]), SETTINGS)

    def test_author_doi_links_remain_untouched(self):
        changed, _ = update_html(ORIGINAL, OfflineCrossref(), SETTINGS)
        self.assertIn('10.1080/00927872.2026.2689550', changed)
        self.assertIn('10.1142/S0219498826502683', changed)

    def test_pinned_doi_must_still_be_verified(self):
        overrides = Overrides(SETTINGS.aliases_by_arxiv, {'2608.27435': FREE['DOI']})
        changed, stats = update_html(ORIGINAL, OfflineCrossref([FREE]), overrides)
        self.assertEqual(stats.promoted, 1)
        wrong = copy.deepcopy(FREE)
        wrong['author'] = [{'family': 'Smith'}]
        unchanged, stats2 = update_html(ORIGINAL, OfflineCrossref([wrong]), overrides)
        self.assertEqual(unchanged, ORIGINAL)
        self.assertEqual(stats2.skipped_ambiguous, 1)

    def test_skip_does_not_search_selected_preprint(self):
        skip = Overrides(SETTINGS.aliases_by_arxiv, {}, frozenset({'2608.27435'}))
        changed, stats = update_html(ORIGINAL, OfflineCrossref([FREE]), skip)
        self.assertEqual(changed, ORIGINAL)
        self.assertEqual(stats.checked_preprints, 3)

    def test_config_json_valid(self):
        cfg = json.loads((ROOT/'scripts'/'publication_overrides.json').read_text())
        self.assertIn('aliases_by_arxiv', cfg)


if __name__ == '__main__':
    unittest.main()
