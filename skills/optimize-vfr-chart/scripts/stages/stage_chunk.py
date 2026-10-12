"""Group drawing blocks by location into Form XObjects so viewers can skip off-screen content.

1. Within each run of consecutive `q 1 0 0 1 X Y cm <path> <paint> Q` blocks (one graphics state),
   shapes are bucketed by location and size class. Reordering inside a run is safe only when all
   shapes paint the same opaque colour: S (stroke colour), f/f* (fill colour), B/B* when fill and
   stroke colours are equal. Other B/B* runs only move a shape past buckets it does not overlap.
   Every shape stays its own verbatim block: merging shapes into one path would change how
   overlapping edges are anti-aliased.
2. Buckets with enough content become Form XObjects (no state changes inside) whose BBox is the
   union of the shapes' control-point boxes padded by stroke reach + PAD, invoked with `Do`.
3. Innermost clip groups `q <path> W* n ... Q` whose clip box is small are wrapped as Forms too,
   with BBox = clip path box + PAD (nothing inside can paint outside its clip). Each such form's
   Resources list only the bucket forms it invokes.

usage: stage_chunk.py in.pdf out.pdf CELL_pt FORM_MIN_bytes
"""
import re
import sys
import zlib

import pikepdf

sys.path.insert(0, __file__.rsplit('/', 1)[0])
from blocks import BLOCK, ColorState, convert, read_content, save_with_content  # noqa: E402

PAD = 20.0
NUM = re.compile(rb'-?(?:\d+\.?\d*|\.\d+)')
DO = re.compile(rb'(/Ck\d+) Do')


def main():
    src, dst = sys.argv[1], sys.argv[2]
    cell, form_min = float(sys.argv[3]), int(sys.argv[4])
    pdf = pikepdf.open(src)
    page = pdf.pages[0]
    assert '/ExtGState' not in page.Resources
    data = read_content(page)
    xobjs = page.Resources.XObject
    forms = {'n': 0}
    st = ColorState()
    out, pos = [], 0
    run = []
    stats = dict(blocks=0, bucketed=0, bucket_forms=0, inline_buckets=0, clip_forms=0, emc_moved=0)

    def make_form(content, bbox):
        used = sorted({m.group(1).decode() for m in DO.finditer(content)})
        fx = pikepdf.Stream(pdf, zlib.compress(content, 9))
        fx.Filter = pikepdf.Name.FlateDecode
        fx.Type, fx.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
        fx.BBox = [round(v, 3) for v in bbox]
        fx.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary({n: xobjs[n] for n in used})) if used \
            else pikepdf.Dictionary()
        name = f'/Ck{forms["n"]}'
        forms['n'] += 1
        xobjs[name] = pdf.make_indirect(fx)
        return name.encode()

    def emit_run_segment(seg, commutative=True):
        reach = max(st.w, 1.0) * 5 + PAD  # >= w/2 * miter limit 10, plus padding
        AA = 5.0  # extra separation so shapes that trade places never share an anti-aliased pixel
        blist, latest = [], {}  # buckets in emission order: [members, inflated union bbox]
        for path, (x0, y0, x1, y1), raw in seg:
            size = max(x1 - x0, y1 - y0)
            level = 0
            while size > cell * (2 ** level):
                level += 1
            c = cell * (2 ** level)
            key = (level, int(((x0 + x1) / 2) // c), int(((y0 + y1) / 2) // c))
            ib = (x0 - reach - AA, y0 - reach - AA, x1 + reach + AA, y1 + reach + AA)
            K = latest.get(key)
            if K is not None and not commutative:
                # joining bucket K moves this shape before every bucket created after K:
                # only allowed if it overlaps none of them
                if len(blist) - K > 64 or any(ib[0] < b[1][2] and b[1][0] < ib[2] and ib[1] < b[1][3] and b[1][1] < ib[3]
                                              for b in blist[K + 1:]):
                    K = None
            if K is None:
                blist.append([[], ib])
                K = latest[key] = len(blist) - 1
            b = blist[K]
            b[0].append((path, (x0, y0, x1, y1), raw))
            b[1] = (min(b[1][0], ib[0]), min(b[1][1], ib[1]), max(b[1][2], ib[2]), max(b[1][3], ib[3]))
        stats['bucketed'] += len(seg)
        for members, _ in blist:
            body = b''.join(raw for _, _, raw in members)
            if len(body) >= form_min and st.ctm_identity:
                bb = [min(m[1][0] for m in members) - reach, min(m[1][1] for m in members) - reach,
                      max(m[1][2] for m in members) + reach, max(m[1][3] for m in members) + reach]
                out.append(b'\n' + make_form(body, bb) + b' Do\n')
                stats['bucket_forms'] += 1
            else:
                out.append(b'\n' + body)
                stats['inline_buckets'] += 1

    def flush():
        i = 0
        while i < len(run):
            paint = run[i][0]
            j = i
            while j < len(run) and run[j][0] == paint:
                j += 1
            seg = [(p, bb, raw) for _, p, bb, raw in run[i:j]]
            same_colour_bstar = st.fill is not None and st.fill == st.stroke
            if len(seg) < 2:
                out.extend(raw for *_, raw in seg)
            elif paint in (b'S', b'f', b'f*') or (paint in (b'B*', b'B') and same_colour_bstar):
                emit_run_segment(seg)  # one opaque colour: any order paints the same pixels
            elif paint in (b'B*', b'B'):
                emit_run_segment(seg, commutative=False)
            else:
                out.extend(raw for *_, raw in seg)
            i = j
        run.clear()

    # CTM tracking (outside blocks): only identity is supported for forms
    st.ctm_identity = True
    ctm_stack = []
    cm_re = re.compile(rb'(?<=\s)(q|Q|cm)(?=\s)')

    def track_ctm(buf):
        for m in cm_re.finditer(buf):
            t = m.group(1)
            if t == b'q':
                ctm_stack.append(st.ctm_identity)
            elif t == b'Q':
                st.ctm_identity = ctm_stack.pop() if ctm_stack else True
            else:
                nums = NUM.findall(buf[max(0, m.start() - 80):m.start()])[-6:]
                if [float(v) for v in nums] != [1, 0, 0, 1, 0, 0]:
                    st.ctm_identity = False

    for m in BLOCK.finditer(data):
        stats['blocks'] += 1
        if m.start() != pos:
            flush()
            gap = data[pos:m.start()]
            out.append(gap)
            st.feed(gap)
            track_ctm(b' ' + gap + b' ')
        pos = m.end()
        conv = convert(m.group(1), m.group(2), m.group(3))
        if conv is None:
            flush()
            out.append(m.group(0))
            continue
        path, bb, _ = conv
        run.append((m.group(4), path, bb, m.group(0)))
    flush()
    tail = data[pos:]
    out.append(tail)
    st.feed(tail)
    assert not st.stack
    new = b''.join(out)

    # --- pass 2: wrap innermost small clip groups as forms ---
    res2, last = [], 0
    qq = re.compile(rb'(?<=\s)(q|Q|BT|ET|BDC|EMC|W\* n|cm|/(?!Ck)[^\s/]+ Do|BI)(?=\s)')
    for w in re.finditer(rb'W\* n', new):
        q = new.rfind(b'q', 0, w.start())
        if q < last or new[q - 1:q] not in (b' ', b'\n') or new[q + 1:q + 2] not in (b' ', b'\n'):
            continue
        clip = new[q + 2:w.start()]
        if not re.fullmatch(rb'(?:-?[\d.]+|[mlch]|\s)+', clip):
            continue
        # find the matching Q; require no text/marked content/other clips/cm inside
        depth, end, ok, emc_at = 1, None, True, None
        for t in qq.finditer(new, w.end()):
            k = t.group(1)
            if k == b'q':
                depth += 1
            elif k == b'Q':
                depth -= 1
                if depth == 0:
                    end = t.start()
                    break
            elif k == b'EMC' and depth == 1 and re.match(rb'\s*Q(?=\s)', new[t.end():t.end() + 16]):
                # `... EMC Q`: the marked section ends just before this group's restore. Moving EMC
                # after the Q is equivalent (Q paints nothing), which makes the group wrappable.
                emc_at = t.start()
            elif k in (b'BT', b'ET', b'BDC', b'EMC', b'W* n', b'BI') or k.endswith(b' Do') or (k == b'cm' and depth == 1):
                ok = False
                break
        if not ok or end is None:
            continue
        v = [float(x) for x in NUM.findall(clip)]
        bx = [min(v[0::2]) - PAD, min(v[1::2]) - PAD, max(v[0::2]) + PAD, max(v[1::2]) + PAD]
        if end - q < form_min:
            continue
        inner = new[q + 1:emc_at if emc_at is not None else end]  # between q and Q (or the moved EMC)
        res2.append(new[last:q])
        res2.append(make_form(inner, bx) + b' Do' + (b'\nEMC' if emc_at is not None else b''))
        last = end + 1
        stats['emc_moved'] += emc_at is not None
        stats['clip_forms'] += 1
    res2.append(new[last:])
    final = b''.join(res2)
    print('chunk:', stats, f'forms {forms["n"]}; content {len(data):,} -> top-level {len(final):,} bytes')
    save_with_content(pdf, page, final, dst)


if __name__ == '__main__':
    main()
