"""Build a fast-opening copy of a large single-page vector chart PDF (ArcGIS/ArcMap export).

usage: optimize_chart.py SOURCE.pdf [OUTPUT.pdf] [--keep-temp]

Default output: <source stem>_optimized.pdf next to the source. The source is only read; its
SHA-256 is checked before and after, and an existing output file is never overwritten.

Stages (each one rewrites drawing blocks only and copies every other byte verbatim):
  cull      drop shapes whose stroke reach + 10 pt misses their clip region (never painted)
  trim      cut 2-point stroke segments to their clip region grown by line width + 10 pt
  fold      merge adjacent clip groups that re-apply the identical clip and graphics state
  split     split long round-joined, round-capped polylines at existing vertices (100 pt cells)
  chunk     group blocks by location into Form XObjects, so viewers skip off-screen content
  assemble  put the new content and forms into the source document graph

This script does not prove the result is equivalent. Run verify_chart.py afterwards.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time

STAGES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stages')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def run(script, *args):
    t = time.time()
    r = subprocess.run([sys.executable, '-I', os.path.join(STAGES, script), *args], capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        print(out)
        raise SystemExit(f'stage {script} failed (exit {r.returncode}); no output written')
    print(f'  [{time.time() - t:5.1f}s] {out.splitlines()[-1] if out else script}', flush=True)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        raise SystemExit(__doc__)
    src = os.path.abspath(args[0])
    dst = os.path.abspath(args[1]) if len(args) > 1 else os.path.splitext(src)[0] + '_optimized.pdf'
    if not os.path.isfile(src):
        raise SystemExit(f'source not found: {src}')
    if os.path.realpath(dst) == os.path.realpath(src):
        raise SystemExit('output path equals the source path; refusing')
    if os.path.exists(dst):
        raise SystemExit(f'output already exists, not overwriting: {dst}')
    before = sha256(src)
    work = tempfile.mkdtemp(prefix='vfr-chart-')
    t0 = time.time()
    print(f'optimizing {src}\n  sha256 {before}', flush=True)
    try:
        p = lambda n: os.path.join(work, n)  # noqa: E731
        run('stage_cull.py', src, p('1_cull.pdf'), '10')
        run('stage_trim.py', p('1_cull.pdf'), p('2_trim.pdf'), '10')
        run('stage_fold.py', p('2_trim.pdf'), p('3_fold.pdf'))
        run('stage_split.py', p('3_fold.pdf'), p('4_split.pdf'), '100')
        run('stage_chunk.py', p('4_split.pdf'), p('5_chunk.pdf'), '100', '4000')
        run('stage_assemble.py', src, p('5_chunk.pdf'), p('6_final.pdf'))
        shutil.copyfile(p('6_final.pdf'), dst + '.partial')
        os.replace(dst + '.partial', dst)
    finally:
        if '--keep-temp' in sys.argv:
            print(f'  intermediate files kept in {work}')
        else:
            shutil.rmtree(work, ignore_errors=True)
    if sha256(src) != before:
        raise SystemExit('the source file changed while optimizing; investigate before using anything')
    a, b = os.path.getsize(src), os.path.getsize(dst)
    print(f'done in {time.time() - t0:.0f}s: {a / 1e6:.1f} MB -> {b / 1e6:.1f} MB\n  output {dst}\n'
          f'  source unchanged (SHA-256 verified)\n  next: verify_chart.py SOURCE OUTPUT')


if __name__ == '__main__':
    main()
