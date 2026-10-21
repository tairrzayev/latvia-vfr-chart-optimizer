"""The visual diff map that render_compare.py writes with --diff-map."""
import json
import re

import numpy as np
import pikepdf
from PIL import Image

from conftest import run


def load_map(d):
    html = (d / 'index.html').read_text()
    return json.loads(re.search(r'<script id="map-data" type="application/json">(.*?)</script>', html, re.S).group(1))


def stitch(d, m, level, layer):
    """One layer of one zoom level, put back together from its tiles."""
    overlay = layer in ('diff', 'strong')
    T, ext = m['tile'], ('png' if overlay else m['ext'])
    out = np.zeros((level['h'], level['w']) + (() if overlay else (3,)), np.uint8)
    for i, name in enumerate(level[layer]):
        if name:
            ty, tx = divmod(i, level['cols'])
            tile = Image.open(d / 'tiles' / f'{name}.{ext}')
            tile = np.asarray(tile if overlay else tile.convert('RGB'))
            out[ty * T:ty * T + tile.shape[0], tx * T:tx * T + tile.shape[1]] = tile
    return out


def test_diff_map_of_an_equivalent_chart(chart, tmp_path):
    src, opt = chart
    code, out = run('render_compare.py', src, opt, '--engines', 'pdfium', '--no-timing', '--out', tmp_path / 'crops',
                    '--diff-map', tmp_path / 'map')
    assert code == 0, out
    m = load_map(tmp_path / 'map')
    e = m['engines']['pdfium']
    assert e['changes'] == [] and e['check']['changes'] == 0
    assert (tmp_path / 'map' / e['overview']).exists()
    assert e['stats']['identical'] + sum(e['stats']['buckets']) == e['stats']['pixels'] == e['width'] * e['height']
    assert e['levels'][-1]['cols'] == e['levels'][-1]['rows'] == 1

    # The finest level holds the exact pixels the render check compared, in the right place.
    import render_compare
    first = render_compare.Pdfium(str(src)).render(3, 0, 0, 500, 400)
    source = stitch(tmp_path / 'map', m, e['levels'][0], 'source')
    assert np.array_equal(source[:first.shape[0], :first.shape[1]], first)
    diff = stitch(tmp_path / 'map', m, e['levels'][0], 'diff')
    assert int((diff > 0).sum()) == sum(e['stats']['buckets'])
    strong = stitch(tmp_path / 'map', m, e['levels'][0], 'strong')
    assert np.array_equal(strong, np.where(diff > 1, diff, 0))
    # Zoomed out, every differing pixel is still shown.
    coarse = stitch(tmp_path / 'map', m, e['levels'][2], 'diff')
    assert coarse.max() == diff.max()


def test_diff_map_marks_a_missing_label(chart, tmp_path):
    src, opt = chart
    bad = tmp_path / 'missing-label.pdf'
    with pikepdf.open(opt) as p:
        page = p.pages[0]
        page.contents_coalesce()
        data = page.Contents.read_bytes()
        assert data.count(b'(RIGA EVRA) Tj') == 1
        page.Contents.write(data.replace(b'(RIGA EVRA) Tj', b'() Tj'))
        p.save(bad)
    code, out = run('render_compare.py', src, bad, '--engines', 'pdfium', '--no-timing', '--out', tmp_path / 'crops',
                    '--diff-map', tmp_path / 'map')
    assert code == 1, out
    e = load_map(tmp_path / 'map')['engines']['pdfium']
    assert e['changes'] and e['check']['changes'] == len(e['changes'])
    assert any(35 < c['x_pt'] < 100 and 25 < c['y_pt'] < 45 for c in e['changes']), e['changes']
    assert e['spots'][0]['peak'] > 64


def test_diff_map_refuses_a_used_directory(chart, tmp_path):
    src, opt = chart
    (tmp_path / 'map').mkdir()
    (tmp_path / 'map' / 'notes.txt').write_text('keep me')
    code, out = run('render_compare.py', src, opt, '--engines', 'pdfium', '--no-timing', '--diff-map', tmp_path / 'map')
    assert code != 0 and 'not empty' in out
    assert (tmp_path / 'map' / 'notes.txt').read_text() == 'keep me'
