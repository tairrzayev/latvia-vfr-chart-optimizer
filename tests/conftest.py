import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, 'skills', 'optimize-vfr-chart', 'scripts')
sys.path.insert(0, SCRIPTS)


def run(script, *args):
    """Run a skill script with this interpreter; return (exit code, combined output)."""
    r = subprocess.run([sys.executable, '-I', os.path.join(SCRIPTS, script), *map(str, args)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


@pytest.fixture(scope='session')
def chart(tmp_path_factory):
    """A synthetic chart and its optimized copy."""
    d = tmp_path_factory.mktemp('chart')
    src = d / 'chart.pdf'
    subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'tests', 'make_chart.py'), str(src)], check=True)
    code, out = run('optimize_chart.py', src)
    assert code == 0, out
    return src, d / 'chart_optimized.pdf'
