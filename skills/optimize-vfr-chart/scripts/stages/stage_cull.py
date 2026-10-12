"""Remove shapes that are entirely outside the active clip path (they can never be painted).

Only `q 1 0 0 1 X Y cm <path> <paint> Q` blocks are candidates. A block is dropped only if its
control-point bbox, inflated by the worst-case stroke reach (w/2 x miter limit 10) plus a safety
margin, misses at least one active clip region. Every kept block and every other byte is copied
verbatim.

usage: stage_cull.py in.pdf out.pdf [margin_pt=10]
"""
import re
import sys

import pikepdf
from shapely.geometry import box

sys.path.insert(0, __file__.rsplit('/', 1)[0])
from blocks import BLOCK, read_content, save_with_content  # noqa: E402
from clipgeom import State, apply  # noqa: E402

MITER = 10.0  # default miter limit; the stage refuses files that set M


def block_bbox(state, m):
    X, Y = float(m.group(1)), float(m.group(2))
    nums = [float(v) for v in re.findall(rb'-?(?:\d+\.?\d*|\.\d+)', m.group(3))]
    xs, ys = nums[0::2], nums[1::2]
    reach = state.w / 2 * MITER
    corners = [apply(state.ctm, X + x, Y + y) for x in (min(xs) - reach, max(xs) + reach)
               for y in (min(ys) - reach, max(ys) + reach)]
    cx, cy = zip(*corners)
    return min(cx), min(cy), max(cx), max(cy)


def cull(data, margin):
    st = State()
    out, pos = [], 0
    kept = dropped = 0
    for m in BLOCK.finditer(data):
        if m.start() != pos:
            gap = data[pos:m.start()]
            out.append(gap)
            st.feed(gap)
        x0, y0, x1, y1 = block_bbox(st, m)
        g = box(x0 - margin, y0 - margin, x1 + margin, y1 + margin)
        if all(c.intersects(g) for c in st.clips):
            out.append(m.group(0))
            kept += 1
        else:
            dropped += 1
        pos = m.end()
    tail = data[pos:]
    out.append(tail)
    st.feed(tail)
    assert not st.stack, 'unbalanced q/Q'
    return b''.join(out), kept, dropped


def main():
    src, dst = sys.argv[1], sys.argv[2]
    margin = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
    pdf = pikepdf.open(src)
    assert len(pdf.pages) == 1, 'expected a single-page chart'
    page = pdf.pages[0]
    assert '/ExtGState' not in page.Resources, 'ExtGState present'
    data = read_content(page)
    assert not re.search(rb'\s-?[\d.]+ M\s', data), 'miter limit set: reach bound would not hold'
    new, kept, dropped = cull(data, margin)
    print(f'cull: margin {margin}pt: kept {kept:,} blocks, dropped {dropped:,}; content {len(data):,} -> {len(new):,} bytes')
    save_with_content(pdf, page, new, dst)


if __name__ == '__main__':
    main()
