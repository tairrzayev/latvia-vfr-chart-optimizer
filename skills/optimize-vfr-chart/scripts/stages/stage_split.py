"""Split long stroked polylines into location-compact pieces (round joins + round caps only).

With line join 1 (round) and line cap 1 (round), stroking a polyline paints exactly the polyline
swept by a disk of diameter w, which equals the union of its pieces stroked separately with round
caps when the pieces share their cut vertices. Pieces reuse the input coordinate text verbatim,
so no coordinate changes. A closing `h` is replaced by an explicit segment back to the start vertex.
Only `q 1 0 0 1 X Y cm <m/l/h only> S Q` blocks under `1 j 1 J` with no dash pattern are touched.

usage: stage_split.py in.pdf out.pdf CELL_pt
"""
import sys

import pikepdf

sys.path.insert(0, __file__.rsplit('/', 1)[0])
from blocks import BLOCK, GState, read_content, save_with_content, tokens  # noqa: E402


class CapState(GState):
    def __init__(self):
        super().__init__()
        self.J = 0
        self.jstack = []

    def feed(self, buf):
        ops = []
        for t in tokens(buf):
            if t == b'q':
                self.jstack.append(self.J)
            elif t == b'Q':
                self.J = self.jstack.pop()
            elif t == b'J' and ops:
                self.J = int(float(ops[-1]))
            elif t == b'd':
                raise SystemExit('dash pattern present: splitting would shift dashes')
            if t[:1] in b'-.0123456789':
                ops.append(t)
            else:
                ops.clear()
        super().feed(buf)


def main():
    src, dst, cell = sys.argv[1], sys.argv[2], float(sys.argv[3])
    pdf = pikepdf.open(src)
    page = pdf.pages[0]
    data = read_content(page)
    st = CapState()
    out, pos = [], 0
    stats = dict(candidates=0, split=0, pieces=0)
    for m in BLOCK.finditer(data):
        if m.start() != pos:
            gap = data[pos:m.start()]
            out.append(gap)
            st.feed(gap)
        pos = m.end()
        body = m.group(3)
        if m.group(4) != b'S' or st.j != 1 or st.J != 1 or b' c ' in b' ' + body:
            out.append(m.group(0))
            continue
        toks = body.split()
        # parse into subpaths of (xs, ys) coordinate strings
        subs, cur, nums = [], None, []
        ok = True
        for t in toks:
            if t == b'm':
                cur = [(nums[0], nums[1])]
                subs.append(cur)
                nums = []
            elif t == b'l':
                if cur is None or len(nums) != 2:
                    ok = False
                    break
                cur.append((nums[0], nums[1]))
                nums = []
            elif t == b'h':
                if cur and cur[-1] != cur[0]:
                    cur.append(cur[0])
                # after h the current point is the subpath start; ArcGIS never continues with l here
                cur = None
            else:
                nums.append(t)
        if not ok or nums:
            out.append(m.group(0))
            continue
        stats['candidates'] += 1
        X, Y = float(m.group(1)), float(m.group(2))
        pieces = []
        for sp in subs:
            if len(sp) < 2:  # degenerate subpath (a round-capped dot); splitting would turn it into a bare moveto
                ok = False
                break
            start = 0
            x0 = x1 = X + float(sp[0][0])
            y0 = y1 = Y + float(sp[0][1])
            for i in range(1, len(sp)):
                x, y = X + float(sp[i][0]), Y + float(sp[i][1])
                nx0, nx1, ny0, ny1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
                if i - start >= 2 and (nx1 - nx0 > cell or ny1 - ny0 > cell):
                    pieces.append(sp[start:i])  # piece ends at vertex i-1; next starts there
                    start = i - 1
                    px, py = X + float(sp[start][0]), Y + float(sp[start][1])
                    nx0, nx1, ny0, ny1 = min(px, x), max(px, x), min(py, y), max(py, y)
                x0, x1, y0, y1 = nx0, nx1, ny0, ny1
            pieces.append(sp[start:])
        if not ok or len(pieces) <= len(subs):
            out.append(m.group(0))  # nothing to split
            continue
        stats['split'] += 1
        for pc in pieces:
            path = b' '.join(pc[0]) + b' m ' + b''.join(x + b' ' + y + b' l ' for x, y in pc[1:])
            out.append(b'q 1 0 0 1 ' + m.group(1) + b' ' + m.group(2) + b' cm ' + path + b'S Q\n')
            stats['pieces'] += 1
    tail = data[pos:]
    out.append(tail)
    st.feed(tail)
    new = b''.join(out)
    print('split:', stats, f'content {len(data):,} -> {len(new):,} bytes')
    save_with_content(pdf, page, new, dst)


if __name__ == '__main__':
    main()
