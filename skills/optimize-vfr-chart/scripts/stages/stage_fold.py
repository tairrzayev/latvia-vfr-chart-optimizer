"""Fold adjacent clip groups that re-apply the identical clip and identical state.

  q <C> W* n <ST> <blocks1> Q  q <C> W* n <ST> <blocks2> Q   ->   q <C> W* n <ST> <blocks1> <blocks2> Q

Exactly equivalent: the clip region is the same (C ∩ C = C), the graphics state seen by blocks2
is the same (ST sets the same values; blocks are self-contained q..Q with no state changes),
and painting order is unchanged.

usage: stage_fold.py in.pdf out.pdf
"""
import re
import sys

import pikepdf

sys.path.insert(0, __file__.rsplit('/', 1)[0])
from blocks import read_content, save_with_content  # noqa: E402

NUM = rb'-?(?:\d+\.?\d*|\.\d+)'
PATHOPS = rb'(?:(?:' + NUM + rb' )+[mlc] |h )+'
BLOCK = rb'q 1 0 0 1 ' + NUM + rb' ' + NUM + rb' cm ' + PATHOPS + rb'(?:B\*|S|f\*|f|B) Q\s*'
PATH_ONLY = re.compile(rb'^(?:' + NUM + rb'|[mlch]|\s)+$')
STATE = re.compile(rb'\s*((?:(?:' + NUM + rb' )+(?:k|K|w|j|J)\s+)*)')
BLOCK_RE = re.compile(BLOCK)
WS = re.compile(rb'\s*')


class Group:
    __slots__ = ('start', 'end', 'clip', 'state', 'blocks')


def groups_linear(data):
    """Yield `q <path> W* n <state> <blocks> Q` groups found by a linear scan."""
    for w in re.finditer(rb'W\* n', data):
        q = data.rfind(b'q', 0, w.start())
        if q < 1 or data[q - 1:q] not in (b' ', b'\n', b'\r') or data[q + 1:q + 2] not in (b' ', b'\n'):
            continue
        clip = data[q + 2:w.start()]
        if not clip or not PATH_ONLY.match(clip):
            continue
        sm = STATE.match(data, w.end())
        pos = sm.end()
        bstart = pos
        n = 0
        while True:
            bm = BLOCK_RE.match(data, pos)
            if not bm:
                break
            pos = bm.end()
            n += 1
        if n == 0 or data[pos:pos + 1] != b'Q' or data[pos + 1:pos + 2] not in (b' ', b'\n', b'\r', b''):
            continue
        g = Group()
        g.start, g.clip, g.state, g.blocks = q, clip, sm.group(1).strip(), data[bstart:pos]
        g.end = WS.match(data, pos + 1).end()
        yield g


def main():
    src, dst = sys.argv[1], sys.argv[2]
    pdf = pikepdf.open(src)
    page = pdf.pages[0]
    data = read_content(page)
    out2, pos = [], 0
    chains, cur = [], []
    for g in groups_linear(data):
        if cur and g.start == cur[-1].end and g.clip == cur[-1].clip and g.state == cur[-1].state:
            cur.append(g)
        else:
            if cur:
                chains.append(cur)
            cur = [g]
    if cur:
        chains.append(cur)
    folded = 0
    for ch in chains:
        out2.append(data[pos:ch[0].start])
        if len(ch) == 1:
            out2.append(data[ch[0].start:ch[0].end])
        else:
            out2.append(b'q ' + ch[0].clip + b'W* n ' + ch[0].state + b' ' +
                        b''.join(g.blocks for g in ch) + b'Q\n')
            folded += len(ch) - 1
        pos = ch[-1].end
    out2.append(data[pos:])
    new = b''.join(out2)
    groups = sum(len(ch) for ch in chains)
    print(f'fold: clip groups matched {groups}, folded away {folded} repeated clips; content {len(data):,} -> {len(new):,} bytes')
    save_with_content(pdf, page, new, dst)


if __name__ == '__main__':
    main()
