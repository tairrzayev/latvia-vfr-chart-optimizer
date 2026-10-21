"""Visual diff map of the source and optimized renders, in the spirit of the failure images of a
golden-image test: the source render is the golden image and the optimized render is compared
with it pixel by pixel.

render_compare.py builds the map from the same tiles it checks (--diff-map DIR), so the map shows
exactly the pixels the render check judged. It writes:

  DIR/index.html            viewer that works offline in any browser: difference view, side by
                            side, swipe and blink, zoomable to single pixels, with the changes
                            the render check found and the places that differ most
  DIR/overview-ENGINE.png   the whole page at a glance: the source faded to grey and every
                            differing pixel coloured by how much it differs
  DIR/tiles/                lossless image tiles; identical tiles are stored once

The viewer can hide differences of 1 or 2 levels: rounding that no one can see, but that covers
whole areas where shapes were moved into groups or lines were shortened outside their clip.

A pixel's difference is the largest difference of its three colour channels, out of 255.
"""
import hashlib
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TILE = 512
OVERVIEW_MAX = 4000  # overview image size limit in pixels
BUCKETS = (  # lowest difference, highest difference, colour
    (1, 2, '#93c5fd'),  # rounding: pale, so stronger differences stand out
    (3, 16, '#f59e0b'),
    (17, 64, '#dc2626'),
    (65, 255, '#c026d3'),
)
ENGINE_LABELS = {'pdfium': 'PDFium (Chrome, Android)', 'mupdf': 'MuPDF',
                 'coregraphics': 'CoreGraphics (Preview, Safari, iOS)'}


def rgb(colour):
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


PALETTE = [0, 0, 0] + [c for _, _, colour in BUCKETS for c in rgb(colour)]
BUCKET_OF = np.zeros(256, np.uint8)
for _k, (_lo, _hi, _) in enumerate(BUCKETS, 1):
    BUCKET_OF[_lo:_hi + 1] = _k


def bucket(d):
    """0 for identical pixels, k for the k-th bucket (d: uint8 differences)."""
    return BUCKET_OF[d]


def halve(img):
    """2x2 mean; with an odd size the last row or column is averaged with itself."""
    H, W = img.shape[:2]
    h, w = H // 2, W // 2
    out = np.empty(((H + 1) // 2, (W + 1) // 2) + img.shape[2:], np.uint8)
    s = img[0:2 * h:2, 0:2 * w:2].astype(np.uint16)
    s += img[1:2 * h:2, 0:2 * w:2]
    s += img[0:2 * h:2, 1:2 * w:2]
    s += img[1:2 * h:2, 1:2 * w:2]
    s += 2
    out[:h, :w] = s // 4
    if W % 2:
        out[:h, w] = (img[0:2 * h:2, W - 1].astype(np.uint16) + img[1:2 * h:2, W - 1] + 1) // 2
    if H % 2:
        out[h, :w] = (img[H - 1, 0:2 * w:2].astype(np.uint16) + img[H - 1, 1:2 * w:2] + 1) // 2
        if W % 2:
            out[h, w] = img[H - 1, W - 1]
    return out


def halve_max(d):
    """2x2 maximum, so a single differing pixel stays visible at every zoom level."""
    H, W = d.shape
    h, w = H // 2, W // 2
    out = np.empty(((H + 1) // 2, (W + 1) // 2), np.uint8)
    m = np.maximum(d[0:2 * h:2, 0:2 * w:2], d[1:2 * h:2, 0:2 * w:2])
    np.maximum(m, d[0:2 * h:2, 1:2 * w:2], out=m)
    np.maximum(m, d[1:2 * h:2, 1:2 * w:2], out=m)
    out[:h, :w] = m
    if W % 2:
        out[:h, w] = np.maximum(d[0:2 * h:2, W - 1], d[1:2 * h:2, W - 1])
    if H % 2:
        out[h, :w] = np.maximum(d[H - 1, 0:2 * w:2], d[H - 1, 1:2 * w:2])
        if W % 2:
            out[h, w] = d[H - 1, W - 1]
    return out


def faded(img):
    """The source in faint grey, the background of the difference view."""
    g = img.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    return np.repeat((255 - (255 - g) * 0.3).astype(np.uint8)[:, :, None], 3, 2)


def top_spots(d, n=25, cell=32):
    """The n places with the largest differences (then the most differing pixels), 5 cells apart.

    Returns (x, y, largest difference, differing pixels in the cell) per place."""
    H, W = d.shape
    ch, cw = -(-H // cell), -(-W // cell)
    p = np.zeros((ch * cell, cw * cell), d.dtype)
    p[:H, :W] = d
    cells = p.reshape(ch, cell, cw, cell)
    peak, count = cells.max((1, 3)).astype(np.int64), (cells > 0).sum((1, 3))
    spots, taken = [], []
    for i in np.lexsort((-count.ravel(), -peak.ravel())):
        if len(spots) == n or peak.flat[i] == 0:
            break
        cy, cx = divmod(int(i), cw)
        if any(abs(cy - y) < 5 and abs(cx - x) < 5 for y, x in taken):
            continue
        taken.append((cy, cx))
        sub = p[cy * cell:(cy + 1) * cell, cx * cell:(cx + 1) * cell]
        iy, ix = np.unravel_index(int(sub.argmax()), sub.shape)
        spots.append((cx * cell + int(ix), cy * cell + int(iy), int(peak.flat[i]), int(count.flat[i])))
    return spots


def describe(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return dict(name=os.path.basename(path), bytes=os.path.getsize(path), sha256=h.hexdigest())


def write_tile(tile, path):
    from PIL import Image
    im = Image.fromarray(tile)
    if tile.ndim == 2:  # difference buckets: palette image, identical pixels transparent
        im.putpalette(PALETTE)
        im.save(path, transparency=0, optimize=True)
    elif path.endswith('.webp'):
        im.save(path, lossless=True, quality=50, method=4)
    else:
        im.save(path, compress_level=6)


class DiffMap:
    """Collects the rendered tiles of each engine and writes the map."""

    def __init__(self, out_dir, source, optimized, region, scale):
        if os.path.exists(out_dir) and (not os.path.isdir(out_dir) or os.listdir(out_dir)):
            raise SystemExit(f'{out_dir} exists and is not empty; choose a new directory for the diff map')
        from PIL import features
        self.dir, self.region, self.scale = out_dir, region, scale
        self.ext = 'webp' if features.check('webp') else 'png'
        self.files = dict(source=describe(source), optimized=describe(optimized))
        self.size = (math.ceil(region[3] * scale) + 2, math.ceil(region[2] * scale) + 2)
        self.engines, self.stored, self.jobs = {}, set(), []
        os.makedirs(os.path.join(out_dir, 'tiles'))

    def start(self, engine):
        self.engine, self.extent, self.changes = engine, [0, 0], []
        self.a = np.full(self.size + (3,), 255, np.uint8)
        self.b = self.a.copy()
        self.d = np.zeros(self.size, np.uint8)

    def origin(self, r):
        """Top-left pixel of the render of region r = (x, y, w, h) in PDF points."""
        x0, y0, w, h = self.region
        return round((y0 + h - r[1] - r[3]) * self.scale), round((r[0] - x0) * self.scale)

    def pt(self, px, py):
        x0, y0, w, h = self.region
        return dict(x_pt=round(x0 + px / self.scale, 1), y_pt=round(y0 + h - py / self.scale, 1))

    def add(self, r, a, b, d):
        y, x = self.origin(r)
        h, w = min(a.shape[0], self.size[0] - y), min(a.shape[1], self.size[1] - x)
        self.a[y:y + h, x:x + w] = a[:h, :w]
        self.b[y:y + h, x:x + w] = b[:h, :w]
        self.d[y:y + h, x:x + w] = d[:h, :w]
        self.extent = [max(self.extent[0], y + h), max(self.extent[1], x + w)]

    def change(self, r, box, kind, peak):
        """A change found by the render check; box = (y0, x0, y1, x1) inside the render of r."""
        y, x = self.origin(r)
        y0, x0, y1, x1 = (int(v) for v in box)
        self.changes.append(dict(kind=kind, peak=round(peak), box=[x + x0, y + y0, x + x1 + 1, y + y1 + 1],
                                 **self.pt(x + (x0 + x1 + 1) / 2, y + (y0 + y1 + 1) / 2)))

    def finish(self, check):
        """Write the tiles and the overview of the current engine; check = render check numbers."""
        H, W = self.extent
        a, b, d = self.a[:H, :W], self.b[:H, :W], self.d[:H, :W]
        self.a = self.b = self.d = None
        counts = sum(np.bincount(bucket(d[y:y + 1024]).ravel(), minlength=len(BUCKETS) + 1) for y in range(0, H, 1024))
        spots = [dict(x=x, y=y, peak=p, pixels=n, **self.pt(x + 0.5, y + 0.5)) for x, y, p, n in top_spots(d)]
        levels, overview = [], None
        with ThreadPoolExecutor(min(16, os.cpu_count() or 4)) as pool:
            while True:
                if overview is None and max(a.shape[:2]) <= OVERVIEW_MAX:
                    overview = self.overview(a, d, 2 ** len(levels))
                levels.append(self.level(pool, a, b, d))
                if max(a.shape[:2]) <= TILE:
                    break
                a, b, d = halve(a), halve(b), halve_max(d)
            for job in self.jobs:
                job.result()
        self.jobs = []
        self.engines[self.engine] = dict(
            label=ENGINE_LABELS.get(self.engine, self.engine), width=W, height=H, levels=levels, overview=overview,
            stats=dict(pixels=int(H * W), identical=int(counts[0]), buckets=[int(c) for c in counts[1:]]),
            check=dict(max_blurred=check['max_blurred'], max_area=check['max_area'], changes=check['blobs']),
            changes=self.changes[:500], spots=spots)
        return os.path.join(self.dir, overview)

    def level(self, pool, a, b, d):
        H, W = d.shape
        rows, cols = -(-H // TILE), -(-W // TILE)
        out = dict(w=W, h=H, cols=cols, rows=rows, source=[], optimized=[], diff=[], strong=[])
        for ty in range(rows):
            for tx in range(cols):
                cut = (slice(ty * TILE, (ty + 1) * TILE), slice(tx * TILE, (tx + 1) * TILE))
                out['source'].append(self.store(pool, a[cut], self.ext))
                out['optimized'].append(self.store(pool, b[cut], self.ext))
                k = bucket(d[cut])
                out['diff'].append(self.store(pool, k, 'png') if k.any() else None)
                k = np.where(k == 1, 0, k).astype(np.uint8)  # the same without 1-2 level differences
                out['strong'].append(self.store(pool, k, 'png') if k.any() else None)
        return out

    def store(self, pool, tile, ext):
        tile = np.array(tile, order='C')  # a copy: the caller may reuse its array while this one is written
        name = hashlib.blake2b(tile.tobytes() + repr(tile.shape).encode(), digest_size=10).hexdigest()
        if (name, ext) not in self.stored:
            self.stored.add((name, ext))
            if len(self.jobs) >= 64:  # bound the tiles waiting in memory
                self.jobs.pop(0).result()
            self.jobs.append(pool.submit(write_tile, tile, os.path.join(self.dir, 'tiles', f'{name}.{ext}')))
        return name

    def overview(self, a, d, factor):
        from PIL import Image, ImageDraw
        img = faded(a)
        k = bucket(d)
        for i, (_, _, colour) in enumerate(BUCKETS, 1):
            img[k == i] = rgb(colour)
        im = Image.fromarray(img)
        draw = ImageDraw.Draw(im)
        for c in self.changes:
            x0, y0, x1, y1 = (v / factor for v in c['box'])
            cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, max(12, (x1 - x0) / 2 + 4), max(12, (y1 - y0) / 2 + 4)
            draw.rectangle([cx - rx, cy - ry, cx + rx, cy + ry], outline=(220, 38, 38), width=3)
        name = f'overview-{self.engine}.png'
        im.save(os.path.join(self.dir, name))
        return name

    def write(self, report):
        data = dict(files=self.files, region=list(self.region), scale=self.scale, tile=TILE, ext=self.ext,
                    buckets=[dict(lo=lo, hi=hi, colour=c) for lo, hi, c in BUCKETS],
                    thresholds=dict(ink=report['threshold'], area=report['area_threshold']), engines=self.engines)
        with open(os.path.join(HERE, 'diff_map.html')) as f:
            html = f.read()
        blob = json.dumps(data, separators=(',', ':')).replace('</', '<\\/')
        path = os.path.join(self.dir, 'index.html')
        with open(path, 'w') as f:
            f.write(html.replace('__MAP_DATA__', blob))
        return path
