#!/usr/bin/env python3
"""Safely promote journal-published preprints on Farukh Mashurov's HTML homepage.

Only the two publication lists within <section id="publications"> are edited;
other sections and unrelated site files are left untouched (except an optional
'Last updated' footer when publication metadata actually changes).

Crossref title + author + journal-article metadata must be an unambiguous match.
Crossref is a source of journal metadata; it is not a source of acceptance data.
This is intentionally stricter than fuzzy scholarly search engines.
No third-party Python packages are required.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import html
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode
from urllib.request import Request, urlopen

SECTION_RE = re.compile(
    r'<section\b[^>]*\bid\s*=\s*["\']publications["\'][^>]*>(.*?)</section\s*>',
    re.IGNORECASE | re.DOTALL,
)
HEADING_RE_TEMPLATE = r'<h3\b[^>]*\bid\s*=\s*["\']%s["\'][^>]*>.*?</h3\s*>'
LIST_RE = re.compile(
    r'<ol\b[^>]*\bclass\s*=\s*["\'][^"\']*\bpub-list\b[^"\']*["\'][^>]*>'
    r'(?P<body>.*?)</ol\s*>', re.IGNORECASE | re.DOTALL,
)
ITEM_RE = re.compile(r'<li\b[^>]*>.*?</li\s*>', re.IGNORECASE | re.DOTALL)
TITLE_RE = re.compile(r'<strong\b[^>]*>(?P<title>.*?)</strong\s*>', re.IGNORECASE | re.DOTALL)
LINKS_RE = re.compile(
    r'(?P<open><span\b[^>]*\bclass\s*=\s*["\'][^"\']*\blinks\b[^"\']*["\'][^>]*>)'
    r'(?P<inside>.*?)(?P<close></span\s*>)', re.IGNORECASE | re.DOTALL,
)
BR_RE = re.compile(r'<br\s*/?>', re.IGNORECASE)
TAG_RE = re.compile(r'<[^>]*>')
STATUS_RE = re.compile(r'\b(?:submitted|preprint|under\s+review|in\s+revision|accepted|to\s+appear)\b\.?', re.IGNORECASE)
ARXIV_ID_RE = re.compile(r'arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?', re.IGNORECASE)
DOI_URL_RE = re.compile(r'https?://(?:dx\.)?doi\.org/(10\.\d{4,9}/[^\s"\'<>]+)', re.IGNORECASE)
AUTO_DOI_RE = re.compile(
    r'<a\b(?=[^>]*\bclass\s*=\s*["\']auto-doi["\'])[^>]*\bhref\s*=\s*["\']https://doi\.org/(10\.[^"\']+)["\'][^>]*>',
    re.IGNORECASE,
)
DOI_VALID_RE = re.compile(r'10\.\d{4,9}/[^\s<>]+', re.IGNORECASE)
CITATION_START = '<!-- AUTO-PUBLICATION:CITATION:START -->'
CITATION_END = '<!-- AUTO-PUBLICATION:CITATION:END -->'
CITATION_RE = re.compile(re.escape(CITATION_START) + r'.*?' + re.escape(CITATION_END), re.DOTALL)
FOOTER_RE = re.compile(r'(?P<before><p>Last updated:\s*)(?:[A-Za-z]+ \d{4})(?P<after>\.</p>)', re.IGNORECASE)


class SiteFormatError(RuntimeError):
    """Raised rather than risk accidental rewriting of unexpected HTML."""


def normalized(s: str) -> str:
    s = html.unescape(TAG_RE.sub('', s))
    s = unicodedata.normalize('NFKC', s).casefold()
    s = s.replace('ı', 'i')
    return re.sub(r'\s+', ' ', re.sub(r'[^\w]+', ' ', s, flags=re.UNICODE)).strip()


def readable_title(li: str) -> str | None:
    match = TITLE_RE.search(li)
    if not match:
        return None
    return html.unescape(TAG_RE.sub('', match.group('title'))).strip()


def arxiv_id(li: str) -> str | None:
    match = ARXIV_ID_RE.search(html.unescape(li))
    return match.group(1) if match else None


def expected_coauthors(li: str) -> set[str]:
    """Parse the site's `with A. Name, B. Name. Submitted.` notation.

    If formatting is unfamiliar, return an empty set; matching still requires
    the exact title and author Mashurov.
    """
    title = TITLE_RE.search(li)
    if not title:
        return set()
    after = li[title.end():]
    span = LINKS_RE.search(after)
    if span:
        after = after[:span.start()]
    start = re.search(r'\bwith\s+', after, flags=re.IGNORECASE)
    if not start:
        return set()
    author_text = after[start.end():]
    # When checking an AUTO-managed journal article, citation markup follows
    # the coauthor sentence and must not be mistaken for extra authors.
    author_text = author_text.split(CITATION_START, 1)[0]
    status = STATUS_RE.search(author_text)
    if status:
        author_text = author_text[:status.start()]
    # The published site consistently uses initials followed by family names.
    surnames = re.findall(r'(?:[A-Z]\.\s*)+([\w\-\'’]+)', html.unescape(author_text), flags=re.UNICODE)
    return {normalized(s) for s in surnames if normalized(s)}


def is_journal_doi(doi: str) -> bool:
    doi = doi.strip().rstrip('.,;')
    return bool(DOI_VALID_RE.fullmatch(doi)) and not doi.lower().startswith('10.48550/arxiv.')


def journal_dois_in_li(li: str) -> set[str]:
    return {unquote(m.group(1)).rstrip('.,;') for m in DOI_URL_RE.finditer(html.unescape(li))
            if is_journal_doi(unquote(m.group(1)).rstrip('.,;'))}


def crossref_title(record: dict) -> str:
    ts = record.get('title') or []
    return ts[0] if isinstance(ts, list) and ts and isinstance(ts[0], str) else ''


def crossref_journal(record: dict) -> str:
    ts = record.get('container-title') or []
    return ts[0] if isinstance(ts, list) and ts and isinstance(ts[0], str) else ''


def match_record(record: dict, titles: list[str], coauthors: set[str]) -> bool:
    if not isinstance(record, dict) or record.get('type') != 'journal-article':
        return False
    doi = record.get('DOI') or ''
    if not isinstance(doi, str) or not is_journal_doi(doi) or not crossref_journal(record):
        return False
    title = normalized(crossref_title(record))
    if not title or title not in {normalized(t) for t in titles if isinstance(t, str)}:
        return False
    families = {normalized(a.get('family', '')) for a in record.get('author', [])
                if isinstance(a, dict) and isinstance(a.get('family'), str)}
    return 'mashurov' in families and coauthors.issubset(families)


def citation_year(record: dict) -> str:
    for kind in ('published', 'published-online', 'published-print', 'issued'):
        parts = (record.get(kind) or {}).get('date-parts') or []
        if parts and parts[0] and isinstance(parts[0][0], int) and 1900 <= parts[0][0] <= 2100:
            return str(parts[0][0])
    return ''


def safe_text(field: object) -> str:
    if isinstance(field, (int, float)):
        field = str(field)
    if not isinstance(field, str):
        return ''
    return html.escape(field.strip(), quote=True)


def citation_html(record: dict) -> str:
    journal = safe_text(crossref_journal(record))
    volume = safe_text(record.get('volume'))
    issue = safe_text(record.get('issue'))
    page = safe_text(record.get('page'))
    article = safe_text(record.get('article-number'))
    year = citation_year(record)
    pieces = [f'<em>{journal}</em>']
    if volume:
        pieces.append(f'<strong>{volume}</strong>' + (f'({issue})' if issue else ''))
    elif issue:
        pieces.append(f'no. {issue}')
    if page:
        pieces.append(page)
    elif article:
        pieces.append(f'Article no. {article}')
    formatted = ', '.join(pieces)
    if year:
        formatted += f', {year}'
    return CITATION_START + formatted + '.' + CITATION_END


def doi_anchor(doi: str) -> str:
    if not is_journal_doi(doi):
        raise ValueError('Invalid journal DOI')
    quoted_doi = quote(doi, safe='/;:_-.()')
    return f'<a class="auto-doi" href="https://doi.org/{html.escape(quoted_doi, quote=True)}">doi</a>'


def ensure_doi_link(li: str, doi: str) -> str:
    if any(known.lower() == doi.lower() for known in journal_dois_in_li(li)):
        return li
    anchor = doi_anchor(doi)
    span = LINKS_RE.search(li)
    if span:
        inner = span.group('inside')
        connector = ' | ' if inner.strip() else ''
        return li[:span.start('inside')] + inner + connector + anchor + li[span.end('inside'):]
    close = re.search(r'</li\s*>\s*$', li, re.IGNORECASE | re.DOTALL)
    if not close:
        raise SiteFormatError('Malformed publication <li>')
    return li[:close.start()] + ' <span class="links">' + anchor + '</span>\n' + li[close.start():]


def promote_li(li: str, record: dict) -> str:
    """Keep all original links/author text, changing the status and adding a DOI."""
    title = TITLE_RE.search(li)
    if not title:
        raise SiteFormatError('Missing <strong> title in preprint')
    tail = li[title.end():]
    link = LINKS_RE.search(tail)
    before_links = tail[:link.start()] if link else tail
    status = STATUS_RE.search(before_links)
    if status:
        a, b = title.end() + status.span()[0], title.end() + status.span()[1]
        new = li[:a] + citation_html(record) + li[b:]
    else:
        # A preprint without a stated status is still eligible if Crossref is a match.
        br = BR_RE.search(before_links)
        insert_at = title.end() + br.end() if br else title.end()
        new = li[:insert_at] + '\n            ' + citation_html(record) + li[insert_at:]
    return ensure_doi_link(new, record['DOI'])


def pretty_moved_li(li: str) -> str:
    """Indent a moved list item consistently without altering text or links."""
    lines = [line.strip() for line in li.splitlines() if line.strip()]
    return ('\n              '.join(lines[:-1]) + '\n            ' + lines[-1]
            if len(lines) > 1 else li)


def managed_doi(li: str) -> str | None:
    m = AUTO_DOI_RE.search(li)
    if not m:
        return None
    doi = unquote(html.unescape(m.group(1))).rstrip('.,;')
    return doi if is_journal_doi(doi) else None


def update_managed_li(li: str, record: dict) -> str:
    m = CITATION_RE.search(li)
    if not m:
        return li
    return li[:m.start()] + citation_html(record) + li[m.end():]


@dataclass(frozen=True)
class Overrides:
    aliases_by_arxiv: dict[str, list[str]] = field(default_factory=dict)
    doi_by_arxiv: dict[str, str] = field(default_factory=dict)
    skip_arxiv: frozenset[str] = frozenset()
    skip_titles: frozenset[str] = frozenset()

    @classmethod
    def load(cls, filename: Path) -> 'Overrides':
        if not filename.exists():
            return cls()
        content = json.loads(filename.read_text(encoding='utf-8'))
        if not isinstance(content, dict):
            raise ValueError('Overrides configuration must be an object')
        known = {'aliases_by_arxiv', 'doi_by_arxiv', 'skip_arxiv', 'skip_titles'}
        if set(content) - known:
            raise ValueError('Unknown configuration keys: ' + str(sorted(set(content) - known)))
        aliases = content.get('aliases_by_arxiv', {})
        pins = content.get('doi_by_arxiv', {})
        skipped_ids = content.get('skip_arxiv', [])
        skipped_titles = content.get('skip_titles', [])
        if not (isinstance(aliases, dict) and isinstance(pins, dict) and
                isinstance(skipped_ids, list) and isinstance(skipped_titles, list)):
            raise ValueError('Invalid configuration data types')
        if not all(isinstance(k, str) and isinstance(v, list) and all(isinstance(a, str) for a in v) for k, v in aliases.items()):
            raise ValueError('Invalid aliases_by_arxiv')
        if not all(isinstance(k, str) and isinstance(v, str) and is_journal_doi(v) for k, v in pins.items()):
            raise ValueError('Invalid DOI overrides')
        if not all(isinstance(s, str) for s in skipped_ids + skipped_titles):
            raise ValueError('Invalid skipped list')
        return cls(aliases, pins, frozenset(skipped_ids), frozenset(normalized(t) for t in skipped_titles))


class Crossref:
    def __init__(self) -> None:
        self.cache: dict[str, dict | None] = {}
        self.mailto = os.getenv('CROSSREF_MAILTO', '').strip()

    def fetch(self, url: str) -> dict | None:
        agent = 'FarukhMashurovHomepageUpdater/2.0 (+https://farukhmashurov.github.io)'
        if self.mailto:
            agent += ' (mailto:' + self.mailto + ')'
        req = Request(url, headers={'Accept': 'application/json', 'User-Agent': agent})
        for retry in range(3):
            try:
                with urlopen(req, timeout=20) as r:
                    return json.load(r)
            except HTTPError as e:
                if e.code == 404:
                    return None
                if e.code not in (429, 500, 502, 503, 504) or retry == 2:
                    print(f'[warning] Crossref HTTP {e.code}', file=sys.stderr)
                    return None
            except (URLError, TimeoutError, ValueError, OSError) as e:
                if retry == 2:
                    print(f'[warning] Crossref unreachable: {e}', file=sys.stderr)
                    return None
            time.sleep(2 ** retry)
        return None

    def by_doi(self, doi: str) -> dict | None:
        key = 'doi:' + doi.casefold()
        if key not in self.cache:
            payload = self.fetch('https://api.crossref.org/works/' + quote(doi, safe='/;:_-.()'))
            self.cache[key] = (payload or {}).get('message')
        return self.cache[key]

    def search(self, titles: list[str], coauthors: set[str]) -> tuple[dict | None, bool]:
        # Search each confirmed title spelling, accepting matches to any.
        candidates: dict[str, dict] = {}
        for title in dict.fromkeys(titles):
            params = {
                'query.bibliographic': title,
                'query.author': 'Mashurov',
                'filter': 'type:journal-article',
                'rows': '25',
            }
            if self.mailto:
                params['mailto'] = self.mailto
            url = 'https://api.crossref.org/works?' + urlencode(params)
            key = 'search:' + normalized(title)
            if key not in self.cache:
                self.cache[key] = self.fetch(url)
            message = (self.cache[key] or {}).get('message') or {}
            for item in message.get('items', []):
                if match_record(item, titles, coauthors):
                    candidates[item['DOI'].casefold()] = item
        if len(candidates) > 1:
            print(f'[review] Multiple journal DOIs for {titles[0]!r}: ' + ', '.join(sorted(candidates)))
            return None, True
        return (next(iter(candidates.values())) if candidates else None), False


@dataclass
class Stats:
    checked_preprints: int = 0
    promoted: int = 0
    refreshed: int = 0
    skipped_ambiguous: int = 0
    pending: int = 0


def find_lists(body: str) -> tuple[re.Match, re.Match]:
    headers = []
    for ident in ('preprints', 'peer-reviewed-journal-articles'):
        header_re = re.compile(HEADING_RE_TEMPLATE % re.escape(ident), re.IGNORECASE | re.DOTALL)
        m = header_re.search(body)
        if not m:
            raise SiteFormatError(f'Heading id="{ident}" not found')
        headers.append(m)
    pre_h, journal_h = headers
    if journal_h.start() <= pre_h.end():
        raise SiteFormatError('Unexpected publication heading order')
    pre_l = LIST_RE.search(body, pre_h.end(), journal_h.start())
    journal_l = LIST_RE.search(body, journal_h.end())
    if not pre_l or not journal_l:
        raise SiteFormatError('Could not find both <ol class="pub-list"> publication lists')
    if pre_l.end() > journal_h.start():
        raise SiteFormatError('The two publication lists overlap')
    return pre_l, journal_l


def update_html(source: str, client: Crossref, overrides: Overrides, *, update_footer: bool = True) -> tuple[str, Stats]:
    sections = list(SECTION_RE.finditer(source))
    if len(sections) != 1:
        raise SiteFormatError(f'Expected one publications section; found {len(sections)}')
    section = sections[0]
    content = section.group(1)
    pre_list, journal_list = find_lists(content)
    pre = pre_list.group('body')
    journals = journal_list.group('body')
    stats = Stats()
    existing_journal_titles = {normalized(readable_title(m.group()) or '') for m in ITEM_RE.finditer(journals)}
    existing_journal_dois = {doi.casefold() for m in ITEM_RE.finditer(journals) for doi in journal_dois_in_li(m.group())}
    # Refresh only previously AUTO-managed entries; manual bibliography is authoritative.
    new_journals = journals
    for m in reversed(list(ITEM_RE.finditer(journals))):
        li = m.group()
        doi = managed_doi(li)
        if not doi or not CITATION_RE.search(li):
            continue
        title = readable_title(li)
        if not title:
            continue
        identifiers = arxiv_id(li)
        aliases = overrides.aliases_by_arxiv.get(identifiers or '', [])
        record = client.by_doi(doi)
        if not record or not match_record(record, [title, *aliases], expected_coauthors(li)):
            continue
        if record['DOI'].casefold() != doi.casefold():
            continue
        rewritten = update_managed_li(li, record)
        if rewritten != li:
            stats.refreshed += 1
            new_journals = new_journals[:m.start()] + rewritten + new_journals[m.end():]
            print(f'[refreshed] {title}: {doi}')

    moved: list[str] = []
    new_pre = pre
    for m in reversed(list(ITEM_RE.finditer(pre))):
        li = m.group()
        title = readable_title(li)
        if not title or len(normalized(title)) < 8:
            continue
        aid = arxiv_id(li)
        if (aid in overrides.skip_arxiv or normalized(title) in overrides.skip_titles):
            continue
        stats.checked_preprints += 1
        if normalized(title) in existing_journal_titles:
            print(f'[review] Duplicate title already appears in journal list: {title}')
            stats.skipped_ambiguous += 1
            continue
        titles = [title, *overrides.aliases_by_arxiv.get(aid or '', [])]
        coauthors = expected_coauthors(li)
        pinned = overrides.doi_by_arxiv.get(aid or '')
        if pinned:
            record = client.by_doi(pinned)
            if not record or not match_record(record, titles, coauthors) or record['DOI'].casefold() != pinned.casefold():
                print(f'[review] DOI override could not be validated: {title}')
                stats.skipped_ambiguous += 1
                continue
            ambiguous = False
        else:
            record, ambiguous = client.search(titles, coauthors)
        if ambiguous:
            stats.skipped_ambiguous += 1
            continue
        if not record:
            stats.pending += 1
            print(f'[pending] {title}')
            continue
        doi = record['DOI']
        if doi.casefold() in existing_journal_dois:
            stats.skipped_ambiguous += 1
            print(f'[review] DOI already listed in journal section: {doi}')
            continue
        moved.insert(0, pretty_moved_li(promote_li(li, record)))
        # Drop publication item and indentation before it, but retain line ending.
        line_start = pre.rfind('\n', 0, m.start()) + 1
        if pre[line_start:m.start()].strip():
            line_start = m.start()
        line_end = m.end()
        if new_pre[line_end:line_end + 2] == '\r\n':
            line_end += 2
        elif new_pre[line_end:line_end + 1] == '\n':
            line_end += 1
        new_pre = new_pre[:line_start] + new_pre[line_end:]
        stats.promoted += 1
        existing_journal_titles.add(normalized(title))
        existing_journal_dois.add(doi.casefold())
        print(f'[published] {title}: {doi}')

    if moved:
        # Insert promoted entries first, at the beginning of the existing journal list.
        first_item = ITEM_RE.search(new_journals)
        if not first_item:
            new_journals += '\n            ' + '\n            '.join(moved) + '\n'
        else:
            new_journals = (new_journals[:first_item.start()] +
                            ('\n            '.join(moved)) + '\n            ' +
                            new_journals[first_item.start():])

    # Both body replacements use offsets from original content, not rewritten offsets.
    replaced_content = (content[:pre_list.start('body')] + new_pre +
                        content[pre_list.end('body'):journal_list.start('body')] +
                        new_journals + content[journal_list.end('body'):])
    updated = source[:section.start(1)] + replaced_content + source[section.end(1):]
    if update_footer and replaced_content != content:
        now = dt.datetime.now(dt.timezone.utc).strftime('%B %Y')
        updated = FOOTER_RE.sub(lambda m: m.group('before') + now + m.group('after'), updated, count=1)
    return updated, stats


def main(args: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--file', default='index.html', help='Website homepage HTML')
    p.add_argument('--overrides', default='scripts/publication_overrides.json', help='Known title variants and pinned verified DOIs')
    p.add_argument('--dry-run', action='store_true', help='Print diff, do not write')
    p.add_argument('--no-footer-update', action='store_true', help='Keep the original last-updated date')
    ns = p.parse_args(args)
    file = Path(ns.file)
    if not file.is_file():
        p.error(f'No such file: {file}')
    try:
        original = file.read_text(encoding='utf-8')
        overrides = Overrides.load(Path(ns.overrides))
        updated, stats = update_html(original, Crossref(), overrides, update_footer=not ns.no_footer_update)
    except (SiteFormatError, ValueError, json.JSONDecodeError) as e:
        print(f'[error] {e}', file=sys.stderr)
        return 2
    if updated != original:
        if ns.dry_run:
            sys.stdout.writelines(difflib.unified_diff(original.splitlines(True), updated.splitlines(True), fromfile=str(file), tofile='proposed'))
        else:
            file.write_text(updated, encoding='utf-8')
    print(f'Checked {stats.checked_preprints} preprints; published {stats.promoted}; '
          f'refreshed {stats.refreshed}; pending {stats.pending}; '
          f'needs review {stats.skipped_ambiguous}; changed={updated != original}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
