"""End to end on a synthetic chart: optimize, verify, plant defects."""
from conftest import run


def test_optimized_chart_passes_verification(chart):
    src, opt = chart
    code, out = run('verify_chart.py', src, opt)
    assert code == 0, out
    assert 'removed_hidden_records' in out and 'trimmed_segments' in out


def test_verifier_catches_every_planted_defect(chart, tmp_path):
    src, opt = chart
    code, out = run('mutation_test.py', src, opt, '--no-render', '--cache', tmp_path)
    assert code == 0, out
    assert out.count('verify: caught') >= 10, out


def test_render_comparison_passes(chart, tmp_path):
    src, opt = chart
    code, out = run('render_compare.py', src, opt, '--engines', 'pdfium,mupdf', '--no-timing', '--out', tmp_path)
    assert code == 0, out


def test_optimizer_refuses_to_overwrite(chart):
    src, opt = chart
    code, out = run('optimize_chart.py', src, opt)
    assert code != 0 and 'not overwriting' in out


def test_verifier_rejects_a_different_chart(chart, tmp_path):
    src, opt = chart
    import pikepdf
    other = tmp_path / 'other.pdf'
    with pikepdf.open(opt) as p:
        p.pages[0].MediaBox = [0, 0, 599, 400]
        p.save(other)
    code, out = run('verify_chart.py', src, other)
    assert code == 1 and 'MediaBox' in out


def test_source_cache_round_trip(chart, tmp_path):
    src, opt = chart
    first = run('verify_chart.py', src, opt, '--cache', tmp_path)
    second = run('verify_chart.py', src, opt, '--cache', tmp_path)
    assert first[0] == 0 and second[0] == 0, second[1]
    assert 'loaded from cache' in second[1]
