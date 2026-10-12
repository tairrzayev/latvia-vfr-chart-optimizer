"""Clip-region geometry and a content-stream state tracker shared by the cull and trim stages.

Clip regions are built lazily with shapely from the clip path (page space). Even-odd clips use
GEOS 'linework' make_valid, which assigns faces by parity; nonzero clips use the union of rings,
a superset of the true region (conservative: content is only dropped when it misses the superset).
"""
import numpy as np
import shapely
from shapely.geometry import Polygon

from blocks import tokens

PAINT_OPS = {b'S', b's', b'f', b'F', b'f*', b'B', b'B*', b'b', b'b*', b'n'}


def mul(a, b):
    """Concatenate affine matrices: result = a then b (PDF order: a x b)."""
    a0, a1, a2, a3, a4, a5 = a
    b0, b1, b2, b3, b4, b5 = b
    return (a0 * b0 + a1 * b2, a0 * b1 + a1 * b3,
            a2 * b0 + a3 * b2, a2 * b1 + a3 * b3,
            a4 * b0 + a5 * b2 + b4, a4 * b1 + a5 * b3 + b5)


def apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def bezier(p0, p1, p2, p3, n=24):
    t = np.linspace(0, 1, n + 1)[1:, None]
    p0, p1, p2, p3 = map(np.array, (p0, p1, p2, p3))
    pts = ((1 - t) ** 3) * p0 + 3 * ((1 - t) ** 2) * t * p1 + 3 * (1 - t) * t * t * p2 + t ** 3 * p3
    return [tuple(p) for p in pts]


def region_from_subpaths(subpaths, evenodd):
    rings = [sp for sp in subpaths if len(set(sp)) >= 3]
    if not rings:
        return shapely.Polygon()
    if evenodd:
        g = shapely.make_valid(Polygon(rings[0], rings[1:]), method='linework')
    else:
        g = shapely.union_all([shapely.make_valid(Polygon(r), method='linework') for r in rings])
    polys = [x for x in shapely.get_parts(g) if x.geom_type in ('Polygon', 'MultiPolygon') and x.area > 0]
    return shapely.union_all(polys) if polys else shapely.Polygon()


class LazyClip:
    def __init__(self, subpaths, evenodd):
        self.subpaths, self.evenodd, self.region = subpaths, evenodd, None

    def intersects(self, g):
        if self.region is None:
            self.region = region_from_subpaths(self.subpaths, self.evenodd)
            shapely.prepare(self.region)
        return self.region.intersects(g)


class Clip:
    """Clip region built lazily; buffered variants cached per distance."""

    def __init__(self, subpaths, evenodd):
        self.subpaths, self.evenodd = subpaths, evenodd
        self._region, self._buffered = None, {}

    @property
    def region(self):
        if self._region is None:
            self._region = region_from_subpaths(self.subpaths, self.evenodd)
            shapely.prepare(self._region)
        return self._region

    def buffered(self, d):
        d = round(d, 3)
        if d not in self._buffered:
            # a quad_segs=4 polygonal buffer lies inside the true buffer by < 2% of d: grow by 3%
            g = self.region.buffer(d * 1.03, quad_segs=4)
            shapely.prepare(g)
            self._buffered[d] = g
        return self._buffered[d]


class State:
    """Tracks CTM, line width, the active clip regions and the current path outside blocks."""

    def __init__(self, clip_class=LazyClip):
        self.clip_class = clip_class
        self.ctm = (1, 0, 0, 1, 0, 0)
        self.w = 1.0
        self.clips = []  # active clip regions (page space), intersected
        self.stack = []
        self.operands = []
        self.path = []  # subpaths in page space
        self.cur = None
        self.pending_clip = None

    def feed(self, buf):
        for t in tokens(buf):
            if t[:1] in b'-.0123456789':
                self.operands.append(float(t))
                continue
            o = self.operands
            if t == b'q':
                self.stack.append((self.ctm, self.w, list(self.clips)))
            elif t == b'Q':
                self.ctm, self.w, self.clips = self.stack.pop()
            elif t == b'cm':
                self.ctm = mul(tuple(o[-6:]), self.ctm)
            elif t == b'w':
                self.w = o[-1]
            elif t == b'm':
                self.cur = [apply(self.ctm, o[-2], o[-1])]
                self.path.append(self.cur)
            elif t == b'l':
                self.cur.append(apply(self.ctm, o[-2], o[-1]))
            elif t in (b'c', b'v', b'y'):
                if t == b'c':
                    p1, p2, p3 = (o[-6], o[-5]), (o[-4], o[-3]), (o[-2], o[-1])
                elif t == b'v':
                    p1, p2, p3 = None, (o[-4], o[-3]), (o[-2], o[-1])
                else:
                    p1, p2, p3 = (o[-4], o[-3]), (o[-2], o[-1]), (o[-2], o[-1])
                p0 = self.cur[-1]
                q1 = apply(self.ctm, *p1) if p1 else p0
                self.cur.extend(bezier(p0, q1, apply(self.ctm, *p2), apply(self.ctm, *p3)))
            elif t == b'h':
                if self.cur:
                    self.cur.append(self.cur[0])
            elif t == b're':
                x, y, wd, ht = o[-4:]
                self.cur = [apply(self.ctm, x, y), apply(self.ctm, x + wd, y),
                            apply(self.ctm, x + wd, y + ht), apply(self.ctm, x, y + ht)]
                self.path.append(self.cur)
            elif t in (b'W', b'W*'):
                self.pending_clip = t == b'W*'
            elif t in PAINT_OPS:
                if self.pending_clip is not None:
                    self.clips.append(self.clip_class(self.path, self.pending_clip))
                    self.pending_clip = None
                self.path, self.cur = [], None
            elif t == b'gs':
                raise SystemExit('ExtGState in content: aborting')
            self.operands = []
