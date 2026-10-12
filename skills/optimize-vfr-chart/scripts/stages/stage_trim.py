"""Trim straight 2-point stroked segments to the active clip regions.

Only blocks of the form `q 1 0 0 1 X Y cm ax ay m bx by l S Q` are touched (no joins, so
cutting a segment into collinear sub-segments cannot change any corner). Each segment is
intersected with every active clip region grown by (w + margin); removed parts and the new
end caps therefore lie >= margin outside the clip and are never painted. Output keeps the
translated-block form (same X, Y), so later stages can treat trimmed pieces like any block.

usage: stage_trim.py in.pdf out.pdf [margin_pt=10]
"""
import re
import sys
import warnings
from decimal import Decimal

import pikepdf
import shapely
from shapely.geometry import LineString

sys.path.insert(0, __file__.rsplit('/', 1)[0])
from blocks import BLOCK, fmt, read_content, save_with_content  # noqa: E402
from clipgeom import Clip, State  # noqa: E402

warnings.filterwarnings('ignore', category=RuntimeWarning)
SEG = re.compile(rb'^(\S+) (\S+) m (\S+) (\S+) l $')


def q4(x):
    return Decimal(f'{x:.4f}')


def main():
    src, dst = sys.argv[1], sys.argv[2]
    margin = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
    pdf = pikepdf.open(src)
    page = pdf.pages[0]
    data = read_content(page)
    assert '/ExtGState' not in page.Resources
    assert not re.search(rb'\s\[[^\]]*\]\s*-?[\d.]+\s+d\s', data), 'dash patterns present'
    st = State(Clip)
    out, pos = [], 0
    stats = dict(segments=0, trimmed=0, pieces=0, untouched=0, failed=0, dropped=0, zero_length=0)
    for m in BLOCK.finditer(data):
        if m.start() != pos:
            gap = data[pos:m.start()]
            out.append(gap)
            st.feed(gap)
        pos = m.end()
        sm = SEG.match(m.group(3))
        if m.group(4) != b'S' or not sm or st.ctm != (1, 0, 0, 1, 0, 0) or not st.clips:
            out.append(m.group(0))
            continue
        stats['segments'] += 1
        X, Y = Decimal(m.group(1).decode()), Decimal(m.group(2).decode())
        a = [Decimal(v.decode()) for v in sm.groups()]
        A = (float(X + a[0]), float(Y + a[1]))
        B = (float(X + a[2]), float(Y + a[3]))
        if A == B:  # zero-length stroke: with round or square caps it paints a dot; keep it
            stats['zero_length'] += 1
            out.append(m.group(0))
            continue
        line = LineString([A, B])
        L = line.length

        def cut(fast):
            g = line
            for c in st.clips:
                reg = c.buffered(st.w + margin)
                if fast:
                    x0, y0, x1, y1 = g.bounds
                    reg = shapely.clip_by_rect(reg, x0 - 1, y0 - 1, x1 + 1, y1 + 1)
                    if not reg.is_valid:
                        raise ValueError('invalid clip_by_rect result')
                g = g.intersection(reg)
                if g.is_empty:
                    break
            return g
        try:
            try:
                g = cut(True)
            except Exception:
                g = cut(False)
            parts = [p for p in shapely.get_parts(g) if p.geom_type == 'LineString' and p.length > 0]
        except Exception:
            stats['failed'] += 1  # keep the segment unchanged
            out.append(m.group(0))
            continue
        if not parts:  # misses every grown clip region: can never be painted
            stats['dropped'] += 1
            continue
        g = shapely.MultiLineString(parts) if len(parts) > 1 else parts[0]
        # express pieces as parameter ranges along A->B
        dx, dy = B[0] - A[0], B[1] - A[1]
        ts = []
        for p in getattr(g, 'geoms', [g]):
            cs = list(p.coords)
            t = sorted(((x - A[0]) * dx + (y - A[1]) * dy) / (L * L) for x, y in (cs[0], cs[-1]))
            ts.append((max(0.0, t[0]), min(1.0, t[1])))
        ts.sort()
        if len(ts) == 1 and ts[0][0] * L < 1e-3 and (1 - ts[0][1]) * L < 1e-3:
            stats['untouched'] += 1
            out.append(m.group(0))
            continue
        stats['trimmed'] += 1
        rdx, rdy = a[2] - a[0], a[3] - a[1]
        for t0, t1 in ts:
            if t1 - t0 <= 0:
                continue
            # a cut within 0.001 pt of an end keeps the exact end point
            p0 = (a[0], a[1]) if t0 * L < 1e-3 else (q4(float(a[0]) + t0 * float(rdx)), q4(float(a[1]) + t0 * float(rdy)))
            p1 = (a[2], a[3]) if (1 - t1) * L < 1e-3 else (q4(float(a[0]) + t1 * float(rdx)), q4(float(a[1]) + t1 * float(rdy)))
            out.append(b'q 1 0 0 1 ' + m.group(1) + b' ' + m.group(2) + b' cm ' +
                       f'{fmt(p0[0])} {fmt(p0[1])} m {fmt(p1[0])} {fmt(p1[1])} l S Q\n'.encode())
            stats['pieces'] += 1
    tail = data[pos:]
    out.append(tail)
    st.feed(tail)
    assert not st.stack
    new = b''.join(out)
    print('trim:', stats, f'content {len(data):,} -> {len(new):,} bytes')
    save_with_content(pdf, page, new, dst)


if __name__ == '__main__':
    main()
