"""Render the source and optimized chart with real PDF engines and compare the pixels.

usage: render_compare.py SOURCE.pdf OPTIMIZED.pdf [--engines pdfium,mupdf,coregraphics]
                         [--scale 3] [--tile 500] [--region x,y,w,h] [--out DIR] [--json FILE]
                         [--diff-map DIR]

Engines: PDFium (Chrome, Android, many apps), MuPDF, and CoreGraphics (Preview, Safari, iOS;
macOS only, built on first use with swiftc). Each engine renders both files over the whole page
(or --region) at --scale (3 = 216 dpi) in tiles, and once at phone-screen width for timing.

Anti-aliasing can differ slightly between files that are geometrically identical (for example a
stroke cut into pieces is rasterised from different end points). Real losses look different: ink
appears or disappears. Two detectors run on every tile:

- ink: both renders are blurred (Gaussian, sigma 1.5 px), which cancels sub-pixel shifts of the
  same ink; a connected area where a colour channel differs by more than --threshold (default 24
  of 255) is a change. Equivalent files stay around 10-13 in all three engines.
- area: the mean signed colour difference over 4 pt squares; more than --area-threshold (default
  12) is a change. This catches a lighter or darker tint over an area, such as a stipple pattern
  that an engine failed to draw.

Every change is reported with a side-by-side crop (source | optimized | difference) for review.
--diff-map DIR also writes a visual diff map of the whole page from the same renders (see
diff_map.py): a zoomable viewer with difference, side-by-side, swipe and blink views, and an
overview image per engine.
Single dots smaller than about a pixel, and colour shifts of a few percent, stay below these
thresholds; verify_chart.py is the check for those.

PASS means no change found in any engine. Exit code 0 PASS, 1 FAIL.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache')), 'optimize-vfr-chart')


def gaussian(img, sigma=1.5):
    r = int(3 * sigma)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    out = img.astype(np.float32)
    for axis in (0, 1):
        pad = [(0, 0)] * out.ndim
        pad[axis] = (r, r)
        p = np.pad(out, pad, mode='edge')
        acc = np.zeros_like(out)
        for i, w in enumerate(k):
            acc += w * np.take(p, range(i, i + out.shape[axis]), axis=axis)
        out = acc
    return out


def blobs(mask):
    """Connected components (8-neighbour) of a boolean mask: list of (size, y0, x0, y1, x1)."""
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return []
    seen = np.zeros(mask.shape, bool)
    found = []
    H, W = mask.shape
    for y, x in zip(ys, xs):
        if seen[y, x]:
            continue
        stack, n, box = [(y, x)], 0, [y, x, y, x]
        seen[y, x] = True
        while stack:
            cy, cx = stack.pop()
            n += 1
            box = [min(box[0], cy), min(box[1], cx), max(box[2], cy), max(box[3], cx)]
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        found.append((n, *box))
    return found


class Pdfium:
    name = 'pdfium'

    def __init__(self, path):
        import pypdfium2 as pdfium
        self.doc = pdfium.PdfDocument(path)
        self.page = self.doc[0]
        self.W, self.H = self.page.get_size()

    def render(self, scale, x, y, w, h):
        img = self.page.render(scale=scale, crop=(x, y, self.W - x - w, self.H - y - h)).to_numpy()
        return img[:, :, :3]


class Mupdf:
    name = 'mupdf'

    def __init__(self, path):
        import pymupdf
        self.fz = pymupdf
        page = pymupdf.open(path)[0]
        self.H = page.rect.height
        self.dl = page.get_displaylist()

    def render(self, scale, x, y, w, h):
        clip = self.fz.Rect(x, self.H - y - h, x + w, self.H - y)
        pix = self.dl.get_pixmap(matrix=self.fz.Matrix(scale, scale), clip=clip, alpha=False)
        return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]


class CoreGraphics:
    name = 'coregraphics'

    def __init__(self, path):
        self.path = path
        self.bin = build_cg()

    def render_many(self, scale, regions):
        tmp = tempfile.mkdtemp(prefix='cgrender-')
        try:
            out = subprocess.run([self.bin, self.path, str(scale), tmp] + [','.join(map(str, r)) for r in regions],
                                 check=True, capture_output=True, text=True).stdout.split('\n')
            imgs, secs = [], 0.0
            for line in filter(None, out):
                i, W, H, dt = line.split()
                secs += float(dt)
                imgs.append(np.fromfile(os.path.join(tmp, f'{i}.rgba'), np.uint8).reshape(int(H), int(W), 4)[:, :, :3])
            return imgs, secs
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def build_cg():
    src = os.path.join(HERE, 'cg', 'cgrender.swift')
    exe = os.path.join(CACHE, 'cgrender')
    if not os.path.exists(exe) or os.path.getmtime(exe) < os.path.getmtime(src):
        os.makedirs(CACHE, exist_ok=True)
        subprocess.run(['swiftc', '-O', '-o', exe, src], check=True)
    return exe


def make_engine(name, path):
    return {'pdfium': Pdfium, 'mupdf': Mupdf, 'coregraphics': CoreGraphics}[name](path)


def tiles(region, tile):
    x0, y0, w, h = region
    out = []
    y = y0
    while y < y0 + h - 1e-6:
        x = x0
        while x < x0 + w - 1e-6:
            out.append((x, y, min(tile, x0 + w - x), min(tile, y0 + h - y)))
            x += tile
        y += tile
    return out


def save_crop(a, b, box, path, pad=40):
    from PIL import Image
    y0, x0, y1, x1 = box
    y0, x0 = max(0, y0 - pad), max(0, x0 - pad)
    y1, x1 = min(a.shape[0], y1 + pad + 1), min(a.shape[1], x1 + pad + 1)
    ca, cb = a[y0:y1, x0:x1], b[y0:y1, x0:x1]
    d = 255 - np.minimum(255, np.abs(ca.astype(np.int16) - cb.astype(np.int16)).max(2) * 4).astype(np.uint8)
    d = np.repeat(d[:, :, None], 3, 2)
    gap = np.full((ca.shape[0], 6, 3), 128, np.uint8)
    Image.fromarray(np.concatenate([ca, gap, cb, gap, d], 1)).save(path)


def area_diff(a, b, block):
    """Largest |mean signed difference| per colour channel over block x block pixel squares."""
    H, W = (a.shape[0] // block) * block, (a.shape[1] // block) * block
    if H == 0 or W == 0:
        return np.zeros((0, 0))
    d = a[:H, :W].astype(np.float32) - b[:H, :W].astype(np.float32)
    return np.abs(d.reshape(H // block, block, W // block, block, 3).mean((1, 3))).max(2)


def compare_engine(name, src, opt, regions, scale, threshold, area_threshold, out_dir, report, dmap=None):
    t = time.time()
    s_eng, o_eng = make_engine(name, src), make_engine(name, opt)
    block = max(4, round(4 * scale))  # 4 pt squares
    stats = dict(tiles=len(regions), pixels=0, px_diff=0, px_diff_gt16=0, px_diff_gt64=0, max_diff=0, max_blurred=0,
                 max_area=0.0, blobs=0)
    worst = []
    if dmap:
        dmap.start(name)
    if name == 'coregraphics':
        def pairs():
            for k in range(0, len(regions), 8):
                part = regions[k:k + 8]
                a, _ = s_eng.render_many(scale, part)
                b, _ = o_eng.render_many(scale, part)
                yield from zip(part, a, b)
    else:
        def pairs():
            for r in regions:
                yield r, s_eng.render(scale, *r), o_eng.render(scale, *r)
    for r, a, b in pairs():
        if a.shape != b.shape:
            raise SystemExit(f'{name}: render sizes differ for region {r}')
        d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(2)
        if dmap:
            dmap.add(r, a, b, d.astype(np.uint8))
        stats['pixels'] += d.size
        stats['px_diff'] += int((d > 0).sum())
        stats['px_diff_gt16'] += int((d > 16).sum())
        stats['px_diff_gt64'] += int((d > 64).sum())
        stats['max_diff'] = max(stats['max_diff'], int(d.max()))
        if not d.any():
            continue
        ys, xs = np.nonzero(d)
        y0, y1, x0, x1 = max(0, ys.min() - 8), ys.max() + 9, max(0, xs.min() - 8), xs.max() + 9
        bd = np.abs(gaussian(a[y0:y1, x0:x1]) - gaussian(b[y0:y1, x0:x1])).max(2)
        ad = area_diff(a, b, block)
        stats['max_blurred'] = max(stats['max_blurred'], int(round(float(bd.max()))))
        stats['max_area'] = max(stats['max_area'], round(float(ad.max()), 1) if ad.size else 0.0)
        found = [(float(bd[p0:p1 + 1, q0:q1 + 1].max()), n, (p0 + y0, q0 + x0, p1 + y0, q1 + x0), 'ink')
                 for n, p0, q0, p1, q1 in blobs(bd > threshold)]
        found += [(float(ad[p0:p1 + 1, q0:q1 + 1].max()), n * block * block,
                   (p0 * block, q0 * block, (p1 + 1) * block - 1, (q1 + 1) * block - 1), 'area')
                  for n, p0, q0, p1, q1 in blobs(ad > area_threshold)]
        for peak, n, (by0, bx0, by1, bx1), what in found:
            if dmap:
                dmap.change(r, (by0, bx0, by1, bx1), what, peak)
            px, py = r[0] + (bx0 + bx1) / 2 / scale, r[1] + r[3] - (by0 + by1) / 2 / scale
            stats['blobs'] += 1
            worst.append((peak, n, px, py, r, (by0, bx0, by1, bx1), a, b, what))
            worst.sort(key=lambda w: -w[0])
            del worst[12:]
    crops = []
    for k, (peak, n, px, py, r, box, a, b, what) in enumerate(worst):
        p = os.path.join(out_dir, f'{name}_{k:02d}_x{px:.0f}_y{py:.0f}.png')
        save_crop(a, b, box, p)
        crops.append(dict(kind=what, x_pt=round(px, 1), y_pt=round(py, 1), peak=round(peak), pixels=n, crop=p))
    del s_eng, o_eng  # release both parsed files before the diff map is encoded
    overview = dmap.finish(stats) if dmap else None
    stats['seconds'] = round(time.time() - t, 1)
    report['engines'][name] = dict(stats, blobs_detail=crops)
    flag = 'OK ' if not stats['blobs'] else 'BAD'
    print(f'  {flag} {name:12} {stats["tiles"]} tiles, {stats["pixels"] / 1e6:.0f} Mpx: differing px {stats["px_diff"]:,}, '
          f'>16 {stats["px_diff_gt16"]:,}, >64 {stats["px_diff_gt64"]:,}, max {stats["max_diff"]}; '
          f'largest blurred {stats["max_blurred"]}, largest 4 pt area mean {stats["max_area"]}; '
          f'changes found: {stats["blobs"]}  ({stats["seconds"]}s)', flush=True)
    for c in crops[:5]:
        print(f'      {c["kind"]} change at x={c["x_pt"]} y={c["y_pt"]} pt, peak {c["peak"]}, {c["pixels"]} px -> {c["crop"]}')
    if overview:
        print(f'      diff map overview -> {overview}', flush=True)


def timing(name, path, width_px=1600):
    """Seconds to open the file and render the whole page at phone-screen width."""
    t = time.time()
    if name == 'coregraphics':
        eng = CoreGraphics(path)
        import pikepdf
        with pikepdf.open(path) as p:
            W, H = (float(v) for v in p.pages[0].MediaBox[2:4])
        eng.render_many(width_px / W, [(0, 0, W, H)])
    else:
        eng = make_engine(name, path)
        W = eng.W if name == 'pdfium' else eng.dl.rect.width
        H = eng.H
        eng.render(width_px / W, 0, 0, W, H)
    return time.time() - t


def main():
    ap = argparse.ArgumentParser(description='Compare renders of the source and optimized chart.')
    ap.add_argument('source')
    ap.add_argument('optimized')
    default_engines = 'pdfium,mupdf' + (',coregraphics' if sys.platform == 'darwin' and shutil.which('swiftc') else '')
    ap.add_argument('--engines', default=default_engines)
    ap.add_argument('--scale', type=float, default=3.0, help='render scale for the comparison (3 = 216 dpi)')
    ap.add_argument('--tile', type=float, default=500.0, help='tile size in points')
    ap.add_argument('--region', help='x,y,w,h in PDF points (default: whole page)')
    ap.add_argument('--threshold', type=float, default=24.0, help='blurred colour difference counted as an ink change')
    ap.add_argument('--area-threshold', type=float, default=12.0, help='mean colour difference over a 4 pt square counted as an area change')
    ap.add_argument('--out', default=None, help='directory for blob crops (default: a new temp dir)')
    ap.add_argument('--json', help='write the report as JSON here')
    ap.add_argument('--no-timing', action='store_true')
    ap.add_argument('--diff-map', metavar='DIR', help='also write a visual diff map (viewer and overview images) to this new directory')
    a = ap.parse_args()
    import pikepdf
    with pikepdf.open(a.source) as p:
        mb = [float(v) for v in p.pages[0].MediaBox]
    region = tuple(float(v) for v in a.region.split(',')) if a.region else (mb[0], mb[1], mb[2] - mb[0], mb[3] - mb[1])
    out_dir = a.out or tempfile.mkdtemp(prefix='render-compare-')
    os.makedirs(out_dir, exist_ok=True)
    regions = tiles(region, a.tile)
    dmap = None
    if a.diff_map:
        sys.path.insert(0, HERE)
        from diff_map import DiffMap
        dmap = DiffMap(a.diff_map, a.source, a.optimized, region, a.scale)
    report = dict(engines={}, timing={}, scale=a.scale, threshold=a.threshold, area_threshold=a.area_threshold,
                  region=region, crops_dir=out_dir)
    engines = [e for e in a.engines.split(',') if e]
    print(f'comparing at {a.scale}x ({a.scale * 72:.0f} dpi), {len(regions)} tiles, engines: {", ".join(engines)}', flush=True)
    for e in engines:
        compare_engine(e, a.source, a.optimized, regions, a.scale, a.threshold, a.area_threshold, out_dir, report, dmap)
    if not a.no_timing:
        print('time to open + render the whole page 1600 px wide:', flush=True)
        for e in engines:
            ts, to = timing(e, a.source), timing(e, a.optimized)
            report['timing'][e] = dict(source_s=round(ts, 2), optimized_s=round(to, 2))
            print(f'  {e:12} source {ts:6.2f}s   optimized {to:6.2f}s   ({ts / max(to, 1e-3):.1f}x faster)', flush=True)
    ok = all(v['blobs'] == 0 for v in report['engines'].values())
    report['status'] = 'PASS' if ok else 'FAIL'
    if dmap:
        report['diff_map'] = dmap.write(report)
        print(f'\ndiff map: {report["diff_map"]} (open it in a browser)')
    print('\nVERDICT:', 'PASS. No engine shows content added or missing.' if ok else
          f'FAIL. Review the crops in {out_dir} (source | optimized | difference).')
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        with open(a.json, 'w') as f:
            json.dump(report, f, indent=1)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
