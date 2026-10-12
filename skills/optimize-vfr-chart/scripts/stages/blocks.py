"""Shared helpers for the optimizer stages: the translated-block pattern, exact number formatting,
a minimal content-stream tokenizer, and graphics-state trackers.

ArcGIS/ArcMap exports draw almost every shape as a self-contained block
    q 1 0 0 1 X Y cm <path> <paint> Q
Every stage only rewrites such blocks (or groups of them) and copies all other bytes verbatim.
"""
import re
from decimal import Decimal

NUM = rb'-?(?:\d+\.?\d*|\.\d+)'
BLOCK = re.compile(
    rb'(?<=\s)q 1 0 0 1 (' + NUM + rb') (' + NUM + rb') cm ((?:(?:' + NUM + rb' )+[mlc] |h )+)(B\*|S|f\*|f|B) Q\s*'
)
FILL_PAINTS = {b'B*', b'f*', b'f', b'B'}
STROKE_PAINTS = {b'B*', b'B', b'S'}
ARITY = {b'm': 2, b'l': 2, b'c': 6}


def fmt(d):
    s = format(d, 'f')
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    if s in ('-0', ''):
        s = '0'
    return s


def convert(xs, ys, body):
    """Return (absolute path bytes, bbox, has_lines) or None if the body is unexpected."""
    X, Y = Decimal(xs.decode()), Decimal(ys.decode())
    out, nums = [], []
    minx = miny = Decimal('Infinity')
    maxx = maxy = Decimal('-Infinity')
    has_lines = False
    for t in body.split():
        if t in ARITY:
            if len(nums) != ARITY[t]:
                return None
            for i in range(0, len(nums), 2):
                ax = X + Decimal(nums[i])
                ay = Y + Decimal(nums[i + 1])
                minx, maxx = min(minx, ax), max(maxx, ax)
                miny, maxy = min(miny, ay), max(maxy, ay)
                out.append(fmt(ax))
                out.append(fmt(ay))
            out.append(t.decode())
            has_lines |= t == b'l'
            nums = []
        elif t == b'h':
            if nums:
                return None
            out.append('h')
        else:
            nums.append(t.decode())
    if nums:
        return None
    return ' '.join(out).encode(), (float(minx), float(miny), float(maxx), float(maxy)), has_lines


# --- minimal content tokenizer, used only on the text between merged runs to track w/j/M ---
DELIM = b'()<>[]{}/%'
WS = b' \t\r\n\f\x00'


def tokens(buf):
    i, n = 0, len(buf)
    while i < n:
        c = buf[i]
        if c in WS:
            i += 1
        elif c == 0x25:  # % comment
            while i < n and buf[i] not in b'\r\n':
                i += 1
        elif c == 0x28:  # ( literal string with nesting and escapes
            depth, i = 1, i + 1
            while i < n and depth:
                if buf[i] == 0x5C:
                    i += 2
                    continue
                if buf[i] == 0x28:
                    depth += 1
                elif buf[i] == 0x29:
                    depth -= 1
                i += 1
            yield b'<str>'
        elif c == 0x3C:  # < hex string or <<
            if buf[i:i + 2] == b'<<':
                i += 2
                yield b'<<'
            else:
                j = buf.index(b'>', i)
                i = j + 1
                yield b'<hex>'
        elif buf[i:i + 2] == b'>>':
            i += 2
            yield b'>>'
        elif c in b'[]{}':
            i += 1
            yield bytes([c])
        else:
            j = i + 1
            if c == 0x2F:  # name
                while j < n and buf[j] not in WS and buf[j] not in DELIM:
                    j += 1
            else:
                while j < n and buf[j] not in WS and buf[j] not in DELIM:
                    j += 1
            yield buf[i:j]
            i = j


class GState:
    def __init__(self):
        self.stack = []
        self.w, self.j, self.M = 1.0, 0, 10.0
        self.operands = []

    def feed(self, buf):
        for t in tokens(buf):
            if t == b'q':
                self.stack.append((self.w, self.j, self.M))
            elif t == b'Q':
                self.w, self.j, self.M = self.stack.pop()
            elif t == b'w' and self.operands:
                self.w = float(self.operands[-1])
            elif t == b'j' and self.operands:
                self.j = int(float(self.operands[-1]))
            elif t == b'M' and self.operands:
                self.M = float(self.operands[-1])
            elif t == b'gs':
                raise SystemExit('ExtGState in content: refusing to merge (state tracking incomplete)')
            if t[:1] in b'-.0123456789':
                self.operands.append(t)
            else:
                self.operands = []

    def reach(self, has_lines):
        """How far a stroke can extend beyond the path's control-point bbox."""
        half = self.w / 2
        return half * self.M if (has_lines and self.j == 0) else half


class ColorState(GState):
    """GState plus fill/stroke colour tracking (these charts only use k/K)."""

    def __init__(self):
        super().__init__()
        self.fill, self.stroke = None, None
        self.cstack = []

    def feed(self, buf):
        ops = []
        for t in tokens(buf):
            if t == b'q':
                self.cstack.append((self.fill, self.stroke))
            elif t == b'Q':
                self.fill, self.stroke = self.cstack.pop()
            elif t == b'k':
                self.fill = tuple(ops[-4:])
            elif t == b'K':
                self.stroke = tuple(ops[-4:])
            elif t in (b'g', b'G', b'rg', b'RG', b'sc', b'SC', b'scn', b'SCN', b'cs', b'CS'):
                raise SystemExit('unexpected colour operator %r' % t)
            if t[:1] in b'-.0123456789':
                ops.append(t)
            else:
                ops.clear()
        super().feed(buf)



def read_content(page):
    """Decoded page content (a Contents array is concatenated with newlines, as viewers do)."""
    import pikepdf
    C = page.obj.Contents
    if isinstance(C, pikepdf.Array):
        return b'\n'.join(c.read_bytes() for c in C)
    return C.read_bytes()


def save_with_content(pdf, page, data, dst):
    """Replace the page content with `data` (Flate level 9) and save an intermediate file."""
    import zlib
    import pikepdf
    s = pikepdf.Stream(pdf, zlib.compress(data, 9))
    s.Filter = pikepdf.Name.FlateDecode
    page.obj.Contents = pdf.make_indirect(s)
    pdf.save(dst, compress_streams=True, recompress_flate=False, fix_metadata_version=False,
             object_stream_mode=pikepdf.ObjectStreamMode.generate)
