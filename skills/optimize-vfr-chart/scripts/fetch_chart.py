"""List or download the VFR chart PDFs published on an AIS web page.

usage: fetch_chart.py [--list] [--issue current|next|DDMMMYYYY] [--out-dir DIR] [--page URL]

By default the Latvian AIS page (https://ais.lgs.lv/page/VFR_Suport) is read. It lists the
currently effective chart under "CURRENTLY EFFECTIVE" and upcoming issues under "NEXT ISSUE",
each with a "WEF" (with effect from) date. The page's own label is cross-checked against the
dates: the current issue must be the latest one whose WEF date is today or earlier. If the two
disagree the script stops and asks for an explicit --issue.

The download goes to DIR (default: the current directory) under the published file name, via a
.partial file, and is checked to be a complete PDF. An existing file is never overwritten.
The last line of output is a JSON summary for scripting.
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_PAGE = 'https://ais.lgs.lv/page/VFR_Suport'
UA = 'optimize-vfr-chart/1.0 (VFR chart downloader; python-urllib)'
MONTHS = {m: i + 1 for i, m in enumerate('JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split())}
LABELS = {'CURRENTLY EFFECTIVE': 'current', 'NEXT ISSUE': 'next'}


def parse_date(text):
    m = re.search(r'(\d{1,2})\s*([A-Z]{3})\s*(\d{4})', text.upper())
    if not m or m.group(2) not in MONTHS:
        return None
    return dt.date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))


def parse_issues(page_html, base_url):
    """Dated chart PDF links in page order, with their WEF date and the section label above them."""
    token = re.compile(r'<a\s[^>]*href="([^"]+\.pdf)"[^>]*>(.*?)</a>|(' + '|'.join(LABELS) + ')', re.I | re.S)
    issues, label = [], None
    for m in token.finditer(page_html):
        if m.group(3):
            label = LABELS[m.group(3).upper()]
            continue
        href = html.unescape(m.group(1))
        text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html.unescape(m.group(2)))).strip()
        url = urllib.parse.urljoin(base_url, urllib.parse.quote(href, safe='/:%?=&'))
        name = os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path))
        wef = parse_date(text) or parse_date(name)
        if wef is None or any(i['url'] == url for i in issues):
            continue  # undated PDFs (manuals, forms) are not chart issues
        issues.append(dict(name=name, url=url, title=text, wef=wef.isoformat() if wef else None, label=label))
    return issues


def pick(issues, which, today):
    dated = [i for i in issues if i['wef']]
    by_date = [i for i in dated if i['wef'] <= today.isoformat()]
    by_date = max(by_date, key=lambda i: i['wef']) if by_date else None
    by_label = next((i for i in issues if i['label'] == 'current'), None)
    if which == 'current':
        if by_label and by_date and by_label['url'] != by_date['url']:
            raise SystemExit(f'the page labels {by_label["name"]} as currently effective, but by WEF dates it is '
                             f'{by_date["name"]}; choose explicitly with --issue')
        choice = by_label or by_date
        if choice is None:
            raise SystemExit('could not determine the currently effective chart; choose with --issue')
        return choice
    if which == 'next':
        later = sorted((i for i in dated if i['wef'] > today.isoformat()), key=lambda i: i['wef'])
        if not later:
            raise SystemExit('no upcoming issue is listed')
        return later[0]
    want = parse_date(which)
    match = [i for i in issues if want and i['wef'] == want.isoformat()]
    if len(match) != 1:
        raise SystemExit(f'no single issue with WEF {which}; use --list to see the options')
    return match[0]


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except (urllib.error.URLError, TimeoutError) as e:
        raise SystemExit(f'cannot download {url}: {e}')


def download(issue, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, issue['name'])
    data = fetch(issue['url'], timeout=300)
    if not data.startswith(b'%PDF-') or b'%%EOF' not in data[-2048:]:
        raise SystemExit(f'downloaded data is not a complete PDF ({len(data):,} bytes); nothing saved')
    sha = hashlib.sha256(data).hexdigest()
    if os.path.exists(dst):
        with open(dst, 'rb') as f:
            if hashlib.sha256(f.read()).hexdigest() == sha:
                return dst, sha, len(data), 'already downloaded (identical file)'
        raise SystemExit(f'{dst} exists with different content; not overwriting. Move it away or pick --out-dir')
    with open(dst + '.partial', 'wb') as f:
        f.write(data)
    os.replace(dst + '.partial', dst)
    return dst, sha, len(data), 'downloaded'


def main():
    ap = argparse.ArgumentParser(description='List or download VFR chart PDFs from an AIS page.')
    ap.add_argument('--page', default=DEFAULT_PAGE, help=f'AIS page listing the charts (default {DEFAULT_PAGE})')
    ap.add_argument('--list', action='store_true', help='only list the published issues')
    ap.add_argument('--issue', default='current', help='current (default), next, or a WEF date like 03SEP2026')
    ap.add_argument('--out-dir', default='.', help='download directory (default: current directory)')
    ap.add_argument('--today', help=argparse.SUPPRESS)  # for tests
    a = ap.parse_args()
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    issues = parse_issues(fetch(a.page).decode('utf-8', 'replace'), a.page)
    if not issues:
        raise SystemExit(f'no chart PDF links found on {a.page}; the page layout may have changed')
    for i in issues:
        state = 'effective now' if i['label'] == 'current' else ('upcoming' if (i['wef'] or '') > today.isoformat() else '')
        print(f'  WEF {i["wef"] or "?":10}  {state:13}  {i["name"]}')
    if a.list:
        print(json.dumps(dict(issues=issues)))
        return
    issue = pick(issues, a.issue, today)
    path, sha, size, how = download(issue, a.out_dir)
    print(f'{how}: {path} ({size / 1e6:.1f} MB, WEF {issue["wef"]})\n  sha256 {sha}')
    upcoming = [i for i in issues if (i['wef'] or '') > (issue['wef'] or '')]
    if upcoming:
        print('  note: newer issues are already published: ' + ', '.join(f'{i["name"]} (WEF {i["wef"]})' for i in upcoming))
    print(json.dumps(dict(path=os.path.abspath(path), sha256=sha, wef=issue['wef'], url=issue['url'],
                          upcoming=[dict(name=i['name'], wef=i['wef']) for i in upcoming])))


if __name__ == '__main__':
    sys.exit(main())
