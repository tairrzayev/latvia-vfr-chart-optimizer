import datetime as dt

import pytest

from fetch_chart import parse_issues, pick

PAGE = '''<html><body><article>
<p>Every new digital issue invalidates the previous chart issue.</p>
<p>CURRENTLY EFFECTIVE: <a href="/uploads/fck/file/VFR charts/VFR_Chart_15th_edition_03SEP2026.pdf"
 target="_blank">VFR Chart 15th edition (WEF: 03 SEP 2026) </a> (~15.1 MB)</p>
<p>NEXT ISSUE: <a href="/uploads/fck/file/VFR charts/VFR_Chart_15th_edition_29OCT2026.pdf">VFR Chart 15th edition
 (WEF: 29 OCT 2026)</a> <a href="/uploads/fck/file/VFR charts/VFR_Chart_15th_edition_26NOV2026.pdf">VFR Chart
 15th edition (WEF: 26 NOV 2026)</a></p>
</article></body></html>'''
BASE = 'https://ais.example/page/VFR_Suport'


def test_parse_issues_reads_links_dates_and_labels():
    issues = parse_issues(PAGE, BASE)
    assert [i['wef'] for i in issues] == ['2026-09-03', '2026-10-29', '2026-11-26']
    assert [i['label'] for i in issues] == ['current', 'next', 'next']
    assert issues[0]['url'] == 'https://ais.example/uploads/fck/file/VFR%20charts/VFR_Chart_15th_edition_03SEP2026.pdf'
    assert issues[0]['name'] == 'VFR_Chart_15th_edition_03SEP2026.pdf'


def test_pick_current_next_and_explicit_date():
    issues = parse_issues(PAGE, BASE)
    today = dt.date(2026, 10, 12)
    assert pick(issues, 'current', today)['wef'] == '2026-09-03'
    assert pick(issues, 'next', today)['wef'] == '2026-10-29'
    assert pick(issues, '26NOV2026', today)['wef'] == '2026-11-26'


def test_label_that_disagrees_with_dates_stops():
    issues = parse_issues(PAGE, BASE)
    with pytest.raises(SystemExit, match='choose explicitly'):
        pick(issues, 'current', dt.date(2026, 11, 1))  # 29 OCT is effective by date, page still says 03 SEP


def test_undated_pdfs_are_not_chart_issues():
    page = PAGE.replace('</article>', '<a href="/uploads/fck/ANID_Tools_Manual.pdf">here.</a></article>')
    assert [i['name'] for i in parse_issues(page, BASE)][-1] == 'VFR_Chart_15th_edition_26NOV2026.pdf'
    assert parse_issues('<a href="/x/Manual.pdf">manual</a>', BASE) == []
