#!/usr/bin/env python3
"""Download the publisher's own citation export (BibTeX or RIS) and compare it with a .bib entry.

  publisher_bib.py ID...                     print each publisher export
  publisher_bib.py --bib FILE [--key K...]   compare every entry with a DOI or eprint
  publisher_bib.py --file EXPORT --bib FILE --key K
                                             compare an export saved from the browser

ID is a DOI, a doi.org URL, or arXiv:<id>. Known export endpoints are tried first (Springer
Nature, BMC, and Springer; PLOS; Copernicus; arXiv), then the export link on the article's
landing page (Frontiers and others). Publishers behind a bot check (Elsevier, Wiley, NEJM,
PNAS, APS, OUP, IEEE, ACM, ...) answer 403; the script then names the page whose
"Cite" or "Export citation" button gives the file in a browser.

The export is a second source, not the authority: exports carry their own errors (repeated
consortium members, PDF page ranges in place of article numbers, "Volume 12 - 2022").
Where the export, the registrar record, and the PDF byline disagree, the byline decides the
authors and the PDF the other fields. Standard library only.
"""

import argparse
import html
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

UA = 'Mozilla/5.0 (X11; Linux x86_64) publisher-bib/1.0'
SPRINGER = ('10.1038/', '10.1007/', '10.1186/', '10.1140/', '10.1057/', '10.1365/')
CHALLENGE = re.compile(r'Just a moment|cf-chl|captcha|Access Denied|are you a robot', re.I)
_last = {}


def http(url, accept='*/*', timeout=40):
    """GET url; returns (status or None, final url, text)."""
    host = urllib.parse.urlsplit(url).hostname or ''
    wait = 1.0 - (time.time() - _last.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': accept})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            _last[host] = time.time()
            return r.status, r.geturl(), r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        _last[host] = time.time()
        try:
            body = e.read().decode('utf-8', 'replace')
        except (OSError, ValueError):
            body = ''
        return e.code, e.geturl() or url, body
    except (urllib.error.URLError, OSError) as e:
        return None, url, str(e)


def parse_id(s):
    s = s.strip()
    m = re.match(r'^(?:arxiv:|https?://arxiv\.org/abs/)(.+)$', s, re.I)
    if m:
        return 'arxiv', re.sub(r'v\d+$', '', m.group(1))
    m = re.match(r'^(?:doi:|https?://(?:dx\.)?doi\.org/)?(10\.\S+)$', s, re.I)
    if m:
        d = m.group(1)
        a = re.match(r'10\.48550/arxiv\.(.+)$', d, re.I)
        return ('arxiv', a.group(1)) if a else ('doi', d)
    return None, s


# ---------------------------------------------------------------- text and names

def delatex(s):
    s = re.sub(r'\\[\'"`^~=.uvHckrb]\s*\{?\\?([A-Za-z])\}?',
               lambda m: unicodedata.normalize('NFC', m.group(1) + {
                   "'": '\u0301', '"': '\u0308', '`': '\u0300', '^': '\u0302', '~': '\u0303',
                   '=': '\u0304', '.': '\u0307', 'u': '\u0306', 'v': '\u030c', 'H': '\u030b',
                   'c': '\u0327', 'k': '\u0328', 'r': '\u030a', 'b': '\u0331'}[m.group(0)[1]]), s)
    s = s.replace('\\&', '&').replace('--', '\u2013')
    s = re.sub(r'\\[A-Za-z]+\s*', '', s)
    return re.sub(r'\s+', ' ', s.replace('{', '').replace('}', '')).strip()


def fold(s):
    s = unicodedata.normalize('NFKD', delatex(s or '')).replace('\u2019', "'")
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', ' ', s.lower()).strip()


def _balanced(text, i):
    depth = 0
    for j in range(i, len(text)):
        depth += (text[j] == '{') - (text[j] == '}')
        if depth == 0:
            return j
    return len(text) - 1


def parse_fields(body):
    fields, i, n = {}, 0, len(body)
    pat = re.compile(r'[\s,]*([A-Za-z][\w\-:.]*)\s*=\s*')
    while i < n:
        m = pat.match(body, i)
        if not m:
            break
        name, i = m.group(1).lower(), m.end()
        if i < n and body[i] == '{':
            j = _balanced(body, i)
            val, i = body[i + 1:j], j + 1
        elif i < n and body[i] == '"':
            j, depth = i + 1, 0
            while j < n and not (body[j] == '"' and depth == 0):
                depth += (body[j] == '{') - (body[j] == '}')
                j += 1
            val, i = body[i + 1:j], j + 1
        else:
            m2 = re.compile(r'[^,}\s]+').match(body, i)
            val, i = (m2.group(0), m2.end()) if m2 else ('', i + 1)
        fields[name] = re.sub(r'\s+', ' ', val).strip()
    return fields


def parse_bib(text):
    out, i = [], 0
    while True:
        j = text.find('@', i)
        if j < 0:
            return out
        m = re.compile(r'@\s*([A-Za-z]+)\s*\{').match(text, j)
        if not m:
            i = j + 1
            continue
        end = _balanced(text, m.end() - 1)
        if m.group(1).lower() not in ('comment', 'preamble', 'string'):
            key, _, rest = text[m.end():end].partition(',')
            out.append(dict(key=key.strip(), fields=parse_fields(rest)))
        i = end + 1


def parse_ris(text):
    f, au = {}, []
    for line in text.splitlines():
        m = re.match(r'([A-Z][A-Z0-9])  -\s?(.*)$', line)
        if not m:
            continue
        tag, val = m.group(1), m.group(2).strip()
        if tag in ('AU', 'A1'):
            au.append(val)
        elif tag in ('TI', 'T1'):
            f.setdefault('title', val)
        elif tag in ('JO', 'JF', 'T2'):
            f.setdefault('journal', val)
        elif tag == 'VL':
            f['volume'] = val
        elif tag == 'IS':
            f['number'] = val
        elif tag == 'SP':
            f['pages'] = val
        elif tag == 'EP' and f.get('pages'):
            f['pages'] += '--' + val
        elif tag in ('PY', 'Y1'):
            f.setdefault('year', val[:4])
        elif tag == 'DO':
            f['doi'] = val
    f['author'] = ' and '.join(au)
    return f


def parse_export(text):
    t = text.lstrip('\ufeff').strip()
    if t.startswith('@'):
        es = parse_bib(t)
        if es:
            f = es[0]['fields']
            f['author'] = re.sub(r'\s+AND\s+', ' and ', f.get('author', ''))
            return f
    if re.match(r'TY  -', t):
        return parse_ris(t)
    return None


def split_names(s):
    out, depth, cur = [], 0, ''
    for t in re.split(r'(\{|\}|\s+and\s+)', s or ''):
        if t == '{':
            depth += 1
        elif t == '}':
            depth -= 1
        if t and re.fullmatch(r'\s+and\s+', t) and depth == 0:
            out.append(cur.strip())
            cur = ''
        else:
            cur += t or ''
    if cur.strip():
        out.append(cur.strip())
    return [n.strip(' ,') for n in out if n.strip(' ,')]


def name_split(raw):
    """(family key, given name); a braced or comma-less multi-word name is a group."""
    r = raw.strip()
    if (r.startswith('{') and r.endswith('}')) or (',' not in r and len(r.split()) >= 3):
        return fold(r), ''
    if ',' in r:
        parts = [x.strip() for x in r.split(',')]
        return fold(parts[0]), delatex(parts[-1])
    toks = delatex(r).split()
    return fold(toks[-1]), ' '.join(toks[:-1])


# ---------------------------------------------------------------- export URLs

def known_urls(kind, ident):
    if kind == 'arxiv':
        return [f'https://arxiv.org/bibtex/{ident}']
    d = ident.lower()
    if d.startswith(SPRINGER):
        return [f'https://citation-needed.springer.com/v2/references/{ident}?format=bibtex&flavour=citation']
    if d.startswith('10.1371/journal.'):
        return [f'https://journals.plos.org/plosone/article/citation/bibtex?id={ident}']
    m = re.match(r'10\.5194/([a-z]+)-(\d+)-(\d+)-(\d{4})$', d)
    if m:
        j, v, p, y = m.groups()
        return [f'https://{j}.copernicus.org/articles/{v}/{p}/{y}/{j}-{v}-{p}-{y}.bib']
    return []


def landing_urls(doi):
    """Export links on the landing page; (urls, status, landing url)."""
    st, final, page = http('https://doi.org/' + urllib.parse.quote(doi, safe='/()'), 'text/html')
    if st != 200 or CHALLENGE.search(page[:5000]):
        return [], st, final
    links = [html.unescape(h) for h in re.findall(r'href="([^"]+)"', page)]
    cands = [h for h in links if re.search(r'bibtex|\.bib(\?|$)|downloadcitation|citation-needed|'
                                           r'format=ris|\.ris(\?|$)|/citation/(bibtex|ris)', h, re.I)]
    cands.sort(key=lambda h: 0 if re.search(r'bib', h, re.I) else 1)
    seen, out = set(), []
    for h in cands:
        u = urllib.parse.urljoin(final, h)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out, st, final


def fetch_export(s):
    """(text, url, note): the export, where it came from, or why it is missing."""
    kind, ident = parse_id(s)
    if not kind:
        return None, None, f'not a DOI or arXiv identifier: {s}'
    for u in known_urls(kind, ident):
        st, final, text = http(u, 'application/x-bibtex, application/x-research-info-systems, text/plain')
        if st == 200 and parse_export(text):
            return text, final, 'known endpoint'
    if kind == 'arxiv':
        return None, None, 'arXiv export failed; re-run'
    urls, st, landing = landing_urls(ident)
    for u in urls[:4]:
        st2, final, text = http(u, 'application/x-bibtex, application/x-research-info-systems, text/plain')
        if st2 == 200 and parse_export(text):
            return text, final, 'export link on the landing page'
    browser = (f'open https://doi.org/{ident} in the browser, use its "Cite" or "Export citation" '
               'button (BibTeX or RIS), then compare the file with --file')
    host = urllib.parse.urlsplit(landing).hostname
    if st in (401, 403, 429) or st is None:
        return None, None, f'the publisher blocks scripts (HTTP {st}) at {host}; {browser}'
    return None, None, f'no export link on the landing page at {host}; {browser}'


# ---------------------------------------------------------------- comparison

def norm_pages(p):
    return re.sub(r'\s', '', delatex(p or '')).replace('\u2013', '-').replace('--', '-').lower()


def compare(pf, bf, label):
    out = []
    pa = [name_split(n) for n in split_names(pf.get('author', ''))]
    ba = [name_split(n) for n in split_names(bf.get('author', ''))]
    pk, bk = [k for k, _ in pa], [k for k, _ in ba]
    if pk == bk:
        out.append(('SAME', f'authors: {len(pk)}, same order'))
    else:
        dup = sorted({k for k in pk if pk.count(k) > 1 and bk.count(k) < pk.count(k)})
        if dup:
            out.append(('DIFF', 'the export repeats: ' + ', '.join(dup)))
        extra = [k for k in dict.fromkeys(pk) if k not in bk]
        miss = [k for k in dict.fromkeys(bk) if k not in pk]
        if extra:
            out.append(('DIFF', f'in the export, not in the {label} ({len(extra)}): ' + ', '.join(extra)))
        if miss:
            out.append(('DIFF', f'in the {label}, not in the export ({len(miss)}): ' + ', '.join(miss)))
        if not dup and not extra and not miss:
            out.append(('DIFF', f'same authors, different order ({len(pk)} in the export, {len(bk)} in the {label})'))
    given = dict((k, g) for k, g in ba if g and bk.count(k) == 1)
    for k, g in pa:
        if g and k in given and pk.count(k) == 1 and fold(g) != fold(given[k]):
            out.append(('DIFF', f'given name of {k}: export "{g}", {label} "{given[k]}"'))
    for fld in ('title', 'journal', 'volume', 'number', 'pages', 'year', 'doi'):
        pv, bv = pf.get(fld, ''), bf.get(fld, '')
        if not pv and not bv:
            continue
        if fld == 'pages':
            same = norm_pages(pv) == norm_pages(bv)
        elif fld == 'doi':
            same = pv.lower().strip() == bv.lower().strip()
        else:
            same = fold(pv) == fold(bv)
        if same:
            out.append(('SAME', fld))
        elif not pv:
            out.append(('INFO', f'{fld}: not in the export; {label} "{delatex(bv)}"'))
        else:
            out.append(('DIFF', f'{fld}: export "{delatex(pv)}", {label} "{delatex(bv) or "(none)"}"'))
    return out


def report(key, text, src, note, entry, save):
    print(f'=== {key}' + (f'  ({note}: {src})' if src else ''))
    if text is None:
        print(f'    MISSING {note}')
        return
    if save:
        os.makedirs(save, exist_ok=True)
        ext = 'bib' if text.lstrip('\ufeff').strip().startswith('@') else 'ris'
        path = os.path.join(save, re.sub(r'[^\w.-]+', '_', key) + '.publisher.' + ext)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)
        print(f'    saved {path}')
    if entry is None:
        print(re.sub(r'(?m)^', '    ', text.strip()))
        return
    pf = parse_export(text)
    if not pf:
        print('    MISSING the file is neither BibTeX nor RIS')
        return
    for lvl, msg in compare(pf, entry['fields'], 'bib'):
        print(f'    {lvl:4s} {msg}')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('ids', nargs='*', help='DOIs or arXiv identifiers')
    p.add_argument('--bib', help='compare with the entries of this .bib')
    p.add_argument('--key', nargs='+', help='only these keys of --bib')
    p.add_argument('--file', help='an export saved from the browser (with --bib and one --key)')
    p.add_argument('--save', help='directory for the downloaded exports')
    a = p.parse_args()
    entries = []
    if a.bib:
        entries = parse_bib(open(a.bib, encoding='utf-8').read())
        if a.key:
            keys = {k.lower() for k in a.key}
            entries = [e for e in entries if e['key'].lower() in keys]
    if a.file:
        if len(entries) != 1:
            p.error('--file needs --bib and exactly one --key')
        report(entries[0]['key'], open(a.file, encoding='utf-8', errors='replace').read(), a.file,
               'saved export', entries[0], None)
        return
    if a.ids:
        for s in a.ids:
            text, src, note = fetch_export(s)
            report(s, text, src, note, None, a.save)
        return
    if not entries:
        p.error('give identifiers or --bib')
    for e in entries:
        f = e['fields']
        ident = f.get('doi') or (('arXiv:' + f['eprint']) if f.get('eprint') else '')
        if not ident:
            print(f'=== {e["key"]}\n    SKIP no doi or eprint')
            continue
        text, src, note = fetch_export(ident)
        report(e['key'], text, src, note, e, a.save)


if __name__ == '__main__':
    main()
