"""Prove the checkers catch real defects: plant one defect at a time in copies of the optimized
chart and require verify_chart.py to FAIL every one, and render_compare.py to FAIL the defects
that change a visible area (a label, a group of shapes, a removed clip).

usage: mutation_test.py SOURCE.pdf OPTIMIZED.pdf [--cache DIR] [--workers 3] [--keep DIR]

Defects planted (each skipped with a note if the file has no suitable target):
  drop-dot          delete one fill+stroke dot well inside its clip region
  drop-stroke       delete one visible stroke segment piece inside a clip region
  shorten-stroke    cut a visible trimmed stroke short (removes a painted part)
  drop-text         delete one text-showing operation
  drop-group        delete one Form XObject invocation (a group of nearby shapes)
  move-shape        shift one top-level shape by 0.5 pt
  recolour          change one fill colour by 1%
  duplicate-shape   paint one top-level shape twice (darker anti-aliasing)
  remove-clip       delete one clip path (hidden content becomes visible)
  shrink-form       shrink one Form XObject BBox (content gets clipped)
  reorder-text      move one text object to the start of the page (painted underneath)
  change-layer      move one marked-content section to a different layer
  change-georef     nudge one georeferencing coordinate

A checker that lets a planted defect through is broken, and its PASS on the real file means
nothing. render_compare.py is not expected to see single sub-point dots, 1% colour changes or
0.5 pt moves (anti-aliasing noise is larger); for those its result is shown for information.
Exit code 0 when every required detection happens, 1 otherwise.
"""
import argparse
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

import pikepdf

HERE = os.path.dirname(os.path.abspath(__file__))
NUM = rb'-?(?:\d+\.?\d*|\.\d+)'
BLOCK = re.compile(rb'(?<=\s)q 1 0 0 1 (' + NUM + rb') (' + NUM + rb') cm ((?:(?:' + NUM + rb' )+[mlc] |h )+)(B\*|S|f\*|f|B) Q\s*')
CLIP = re.compile(rb'^\s*((?:(?:' + NUM + rb'|[mlh])\s+)+)W\*? n', re.S)
RENDER_REQUIRED = {'drop-text', 'drop-group', 'remove-clip'}
RENDER_INFO = {'drop-dot', 'drop-stroke', 'move-shape', 'recolour'}


def set_stream(pdf, stream, data):
    stream.write(zlib.compress(data, 9), filter=pikepdf.Name.FlateDecode)


def save(pdf, dst):
    """Save like the optimizer does, so the planted defect is the only difference."""
    pdf.save(dst, compress_streams=True, recompress_flate=False, fix_metadata_version=False,
             object_stream_mode=pikepdf.ObjectStreamMode.generate)


def rings_of(clip_text):
    rings, cur, nums = [], None, []
    for t in clip_text.split():
        if t in (b'm', b'l'):
            p = (float(nums[0]), float(nums[1]))
            if t == b'm':
                cur = [p]
                rings.append(cur)
            else:
                cur.append(p)
            nums = []
        elif t == b'h':
            nums = []
        else:
            nums.append(t)
    return rings


def inside_with_margin(rings, x, y, margin):
    """Even-odd membership of (x, y) and distance to the boundary of the given rings."""
    crossings, dmin = 0, float('inf')
    for ring in rings:
        for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
            if (y0 <= y) != (y1 <= y) and x0 + (y - y0) * (x1 - x0) / (y1 - y0) > x:
                crossings += 1
            dx, dy = x1 - x0, y1 - y0
            L2 = dx * dx + dy * dy
            t = 0 if L2 == 0 else max(0, min(1, ((x - x0) * dx + (y - y0) * dy) / L2))
            dmin = min(dmin, ((x - x0 - t * dx) ** 2 + (y - y0 - t * dy) ** 2) ** 0.5)
    return crossings % 2 == 1 and dmin > margin


def clip_forms(pdf):
    """(name, form, clip rings, invoked bucket forms) for forms whose content starts with a clip."""
    xo = pdf.pages[0].Resources.XObject
    out = []
    for k in xo.keys():
        f = xo[k]
        if f.get('/Subtype') != '/Form':
            continue
        data = f.read_bytes()
        m = CLIP.match(data)
        if not m:
            continue
        subs = [n.decode() for n in re.findall(rb'(/[^\s/]+) Do', data)]
        out.append((k, f, rings_of(m.group(1)), data, subs))
    return out


def bbox_region(form):
    x0, y0, x1, y1 = (float(v) for v in form.BBox)
    return (x0, y0, x1 - x0, y1 - y0)


def mutate(src_path, kind, dst, rng):
    """Write a copy with one planted defect; return (description, region for rendering) or None."""
    pdf = pikepdf.open(src_path)
    page = pdf.pages[0]
    top = page.Contents.read_bytes()
    xo = page.Resources.XObject
    region = None
    if kind == 'control':
        save(pdf, dst)
        return 'unchanged copy, saved the same way as the defects (must PASS)', None
    if kind in ('drop-dot', 'drop-stroke', 'shorten-stroke'):
        want = {'drop-dot': (b'B*', b'B'), 'drop-stroke': (b'S',), 'shorten-stroke': (b'S',)}[kind]
        forms = clip_forms(pdf)
        rng.shuffle(forms)
        for name, f, rings, data, subs in forms:
            for sub in [name] + subs:
                target = xo[sub] if sub != name else f
                body = target.read_bytes()
                cands = [m for m in BLOCK.finditer(b' ' + body) if m.group(4) in want]
                rng.shuffle(cands)
                for m in cands[:200]:
                    v = [float(t) for t in re.findall(NUM, m.group(3))]
                    X, Y = float(m.group(1)), float(m.group(2))
                    pts = [(X + v[i], Y + v[i + 1]) for i in range(0, len(v) - 1, 2)]
                    cx = sum(p[0] for p in pts) / len(pts)
                    cy = sum(p[1] for p in pts) / len(pts)
                    if kind != 'drop-dot' and len(pts) != 2:
                        continue
                    if not inside_with_margin(rings, cx, cy, 3.0):
                        continue
                    s, e = m.start() - 1, m.end() - 1
                    if kind == 'shorten-stroke':
                        (ax, ay), (bx, by) = pts
                        nb = f'{v[0] + (v[2] - v[0]) * 0.25:.4f} {v[1] + (v[3] - v[1]) * 0.25:.4f}'.encode()
                        new = m.group(0).replace(m.group(3), f'{v[0]:.4f} {v[1]:.4f} m '.encode() + nb + b' l ')
                        body2 = body[:max(s, 0)] + new + body[e:]
                    else:
                        body2 = body[:max(s, 0)] + body[e:]
                    set_stream(pdf, target, body2)
                    save(pdf, dst)
                    return f'{kind} at x={cx:.1f} y={cy:.1f} in {sub}', (cx - 15, cy - 15, 30, 30)
        return None
    if kind == 'drop-text' or kind == 'reorder-text':
        # labels: text objects that set their own font, paint a dark colour, and are not area
        # symbols clipped to a polygon (those follow a `W* n` clip directly)
        def label(b):
            k = re.search(rb'(' + NUM + rb') (' + NUM + rb') (' + NUM + rb') (' + NUM + rb') k', b.group(0))
            return (b' Tf' in b.group(0) and k is not None and max(float(v) for v in k.groups()) >= 0.8
                    and not top[max(0, b.start() - 8):b.start()].rstrip().endswith(b'n'))
        bts = [b for b in re.finditer(rb'(?<=\s)BT\s.*?\sET(?=\s)', top, re.S) if label(b)]
        if not bts:
            return None
        m = rng.choice(bts)
        tm = re.search(rb'(' + NUM + rb') (' + NUM + rb') Tm', m.group(0))
        if kind == 'drop-text':  # the whole label
            top2 = top[:m.start()] + top[m.end():]
        else:
            top2 = b'q ' + m.group(0) + b' Q\n' + top[:m.start()] + top[m.end():]
        set_stream(pdf, page.Contents, top2)
        save(pdf, dst)
        if tm:
            x, y = float(tm.group(1)), float(tm.group(2))
            region = (x - 20, y - 30, 200, 80)
        return f'{kind} near {region[:2] if region else "?"}', region
    if kind in ('move-shape', 'recolour', 'duplicate-shape'):
        blocks = [b for b in BLOCK.finditer(top) if b.group(4) in (b'S', b'f', b'f*')]
        if not blocks:
            return None
        m = rng.choice(blocks)
        X, Y = float(m.group(1)), float(m.group(2))
        if kind == 'move-shape':
            new = b'q 1 0 0 1 ' + f'{X + 0.5:.4f} '.encode() + m.group(2) + b' cm ' + m.group(3) + m.group(4) + b' Q\n'
            top2 = top[:m.start()] + new + top[m.end():]
        elif kind == 'duplicate-shape':
            top2 = top[:m.end()] + m.group(0) + top[m.end():]
        else:
            k = list(re.finditer(rb'(' + NUM + rb') (' + NUM + rb') (' + NUM + rb') (' + NUM + rb') (k|K)(?=\s)', top[:m.start()]))
            if not k:
                return None
            c = k[-1]
            vals = [float(t) for t in c.groups()[:4]]
            vals[0] = vals[0] + 0.01 if vals[0] < 0.99 else vals[0] - 0.01
            top2 = top[:c.start()] + (' '.join(f'{t:.4f}' for t in vals) + ' ').encode() + c.group(5) + top[c.end():]
        set_stream(pdf, page.Contents, top2)
        save(pdf, dst)
        return f'{kind} at x={X:.1f} y={Y:.1f}', (X - 20, Y - 20, 40, 40)
    if kind == 'remove-clip':
        forms = [f for f in clip_forms(pdf) if all(float(b) - float(a) <= 600
                                                   for a, b in zip(f[1].BBox[:2], f[1].BBox[2:]))]
        if not forms:
            return None
        name, f, rings, data, subs = rng.choice(forms)
        m = CLIP.match(data)
        set_stream(pdf, f, data[m.end():])
        save(pdf, dst)
        return f'{kind} in {name}', bbox_region(f)
    if kind == 'drop-group':  # the group with the most drawing content: plenty of visible ink
        calls = [m for m in re.finditer(rb'(/[^\s/]+) Do', top) if m.group(1).decode() in xo
                 and xo[m.group(1).decode()].get('/Subtype') == '/Form']
        if not calls:
            return None
        m = max(calls, key=lambda c: len(xo[c.group(1).decode()].read_bytes()))
        set_stream(pdf, page.Contents, top[:m.start()] + top[m.end():])
        save(pdf, dst)
        return f'{kind} {m.group(1).decode()}', bbox_region(xo[m.group(1).decode()])
    if kind == 'shrink-form':
        clipped = {c[0] for c in clip_forms(pdf)}
        forms = [k for k in xo.keys() if xo[k].get('/Subtype') == '/Form' and k not in clipped]
        if not forms:
            return None
        k = rng.choice(forms)
        bb = [float(v) for v in xo[k].BBox]
        xo[k].BBox = [bb[0], bb[1], (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2]
        save(pdf, dst)
        return f'{kind} {k}', None
    if kind == 'change-layer':
        tags = list(re.finditer(rb'/OC (/[^\s/]+) BDC', top))
        names = sorted({t.group(1) for t in tags})
        if len(names) < 2:
            return None
        m = rng.choice(tags)
        other = rng.choice([n for n in names if n != m.group(1)])
        set_stream(pdf, page.Contents, top[:m.start(1)] + other + top[m.end(1):])
        save(pdf, dst)
        return f'{kind} {m.group(1).decode()} -> {other.decode()}', None
    if kind == 'change-georef':
        vp = page.obj.get('/VP')
        if vp is None:
            return None
        meas = vp[0].get('/Measure')
        if meas is None or '/GPTS' not in meas:
            return None
        g = [float(v) for v in meas.GPTS]
        g[0] += 1e-5
        meas.GPTS = pikepdf.Array(g)
        save(pdf, dst)
        return f'{kind} GPTS[0] += 1e-5 degrees', None
    raise ValueError(kind)


KINDS = ['control', 'drop-dot', 'drop-stroke', 'shorten-stroke', 'drop-text', 'drop-group', 'move-shape', 'recolour', 'duplicate-shape',
         'remove-clip', 'shrink-form', 'reorder-text', 'change-layer', 'change-georef']


def main():
    ap = argparse.ArgumentParser(description='Check that the verifiers catch planted defects.')
    ap.add_argument('source')
    ap.add_argument('optimized')
    ap.add_argument('--cache', help='verify_chart.py --cache directory (avoids re-reading the source each time)')
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--keep', help='keep the mutated files in this directory')
    ap.add_argument('--no-render', action='store_true', help='skip the render_compare.py checks')
    ap.add_argument('--only', help='comma-separated defect kinds to plant (default: all)')
    a = ap.parse_args()
    work = a.keep or tempfile.mkdtemp(prefix='mutants-')
    os.makedirs(work, exist_ok=True)
    rng = random.Random(a.seed)
    mutants = []
    kinds = ['control'] + [k for k in a.only.split(',') if k != 'control'] if a.only else KINDS
    for kind in kinds:
        dst = os.path.join(work, f'{kind}.pdf')
        r = mutate(a.optimized, kind, dst, rng)
        if r is None:
            print(f'  -    {kind:16} no suitable target in this file, skipped', flush=True)
            continue
        mutants.append((kind, dst, *r))
    if a.cache is None:
        a.cache = os.path.join(work, 'cache')
    # warm the cache once so parallel workers do not all interpret the source
    print('verifying planted defects (each must FAIL) ...', flush=True)
    py = [sys.executable, '-I']

    def run_verify(m):
        kind, dst, desc, region = m
        t = time.time()
        r = subprocess.run(py + [os.path.join(HERE, 'verify_chart.py'), a.source, dst, '--cache', a.cache],
                           capture_output=True, text=True)
        reason = next((ln.strip() for ln in r.stdout.splitlines() if 'PROBLEM' in ln), r.stdout.strip().splitlines()[-1:] or ['?'])
        res = dict(verify=r.returncode, verify_reason=reason if isinstance(reason, str) else reason[0], seconds=time.time() - t)
        if region and not a.no_render and kind in RENDER_REQUIRED | RENDER_INFO:
            rr = subprocess.run(py + [os.path.join(HERE, 'render_compare.py'), a.source, dst, '--engines', 'pdfium,mupdf',
                                      '--region', ','.join(f'{v:.2f}' for v in region), '--no-timing',
                                      '--out', os.path.join(work, f'crops-{kind}')], capture_output=True, text=True)
            res['render'] = rr.returncode
        return res

    if mutants:
        first = run_verify(mutants[0])  # also fills the cache
        with ThreadPoolExecutor(max(1, a.workers)) as ex:
            rest = list(ex.map(run_verify, mutants[1:]))
        results = [first] + rest
    else:
        results = []
    escaped = 0
    for (kind, dst, desc, region), res in zip(mutants, results):
        if kind == 'control':
            ok_control = res['verify'] == 0
            escaped += not ok_control
            print(f'  {"OK " if ok_control else "BAD"}  {kind:16} {desc}\n         verify: '
                  f'{"PASS" if ok_control else "did not pass (exit %d)" % res["verify"]}; {res["verify_reason"][:150]}', flush=True)
            continue
        caught = res['verify'] == 1
        render_note = ''
        if 'render' in res:
            seen = res['render'] == 1
            if kind in RENDER_REQUIRED:
                render_note = '  render: caught' if seen else '  render: NOT caught'
                caught = caught and seen
            else:
                render_note = '  render: ' + ('caught' if seen else 'below its threshold') + ' (informational)'
        escaped += not caught
        print(f'  {"OK " if caught else "BAD"}  {kind:16} {desc}\n         verify: '
              f'{"caught" if res["verify"] == 1 else "NOT caught (exit %d)" % res["verify"]}{render_note}; {res["verify_reason"][:150]}',
              flush=True)
    ok = mutants and not escaped
    print(f'\nVERDICT: {"PASS. Every planted defect was caught." if ok else "FAIL. The checkers missed planted defects; do not trust their PASS."}')
    if not a.keep:
        shutil.rmtree(work, ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
