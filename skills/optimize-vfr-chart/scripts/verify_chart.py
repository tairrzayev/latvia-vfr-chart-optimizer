"""Prove that an optimized chart PDF shows exactly what the source chart PDF shows.

usage: verify_chart.py SOURCE.pdf OPTIMIZED.pdf [--cache DIR] [--json REPORT.json]

The checker shares no code with the optimizer. Content is parsed with qpdf's tokenizer (via
pikepdf), Form XObjects are expanded, and every painting operation becomes a record:

    kind, style (paint operator, colours, line width/cap/join/miter/dash, text state),
    clip stack (exact clip paths), layer stack (optional-content membership), geometry.

Stroked polylines with round joins and round caps are compared segment by segment, because
their painted area is exactly the union of the individual segment strokes.

PASS requires every check below. Anything the checker cannot model (transparency, shadings,
patterns, inline images, ...) stops it with UNSUPPORTED, and the file must not be used.

  A  Document: catalog (layers, metadata, viewer settings), document info, every page entry
     except Contents/Resources (page boxes, georeferencing, annotations), every resource except
     XObject, and every source XObject are identical. New XObjects may only be Form XObjects.
  B  Every optimized record equals a source record (kind, style, clip stack, layers, geometry)
     with the same multiplicity. The only exception is a trimmed stroke: straight pieces that lie
     on a source segment (within 0.001 pt), share its style, clip and layers, do not overlap,
     and whose cut-away parts are proven hidden (D).
  C  Every source record is present, trimmed as in B, or proven hidden (D).
  D  "Proven hidden" is exact geometry, not sampling: the record's painted area (grown by the
     worst-case stroke reach plus 1 pt) touches no edge of one of its clip paths and lies outside
     that clip path's region under its fill rule.
  E  Paint order: the sequence of style/clip/layer runs is identical, every record stays in its
     run, and inside runs that do not paint one opaque colour, overlapping records keep their
     relative order.
  F  Content moved into Form XObjects lies inside the form BBox (which would otherwise clip it).
  G  Text: every text-showing operation in the same order with the same state, and MuPDF's
     character-level extraction (glyphs, positions, fonts, colours) is identical.

Exit code: 0 PASS, 1 FAIL, 2 UNSUPPORTED or error.
"""
import argparse
import hashlib
import json
import math
import os
import pickle
import sys
import time
from array import array
from collections import Counter, defaultdict, deque

import numpy as np
import pikepdf

AA = 1.0          # pt of clearance required between hidden content and its clip region
TOL_LINE = 1e-3   # pt: max distance of a trimmed piece's endpoints from its source segment
SQRT2 = math.sqrt(2.0)
IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
FILL_OPS = {'f', 'F', 'f*', 'B', 'B*', 'b', 'b*'}
STROKE_OPS = {'S', 's', 'B', 'B*', 'b', 'b*'}
PAINT_OPS = FILL_OPS | STROKE_OPS | {'n'}
TEXT_SHOW = {'Tj', 'TJ', "'", '"'}
K_PATH, K_SEG, K_TEXT, K_IMAGE = 0, 1, 2, 3
KIND_NAME = {K_PATH: 'shape', K_SEG: 'stroke segment', K_TEXT: 'text', K_IMAGE: 'image'}


class Unsupported(Exception):
    pass


def mul(a, b):
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


def digest(obj):
    return hashlib.blake2b(repr(obj).encode(), digest_size=16).digest()


def canon(x, stop=(), seen=None):
    """Object-number-independent form of a PDF object graph. Objects in `stop` (the page
    itself, reached through annotation /P links) are represented by a placeholder."""
    seen = {} if seen is None else seen
    if isinstance(x, pikepdf.Object) and x.is_indirect:
        if x.objgen in stop:
            return ('page',)
        if x.objgen in seen:
            return ('ref', seen[x.objgen])
        seen[x.objgen] = len(seen)
    if isinstance(x, pikepdf.Stream):
        try:
            data = ('decoded', hashlib.sha256(x.read_bytes()).hexdigest())
        except pikepdf.PdfError:
            data = ('raw', hashlib.sha256(x.read_raw_bytes()).hexdigest())
        return ('stream', {str(k): canon(v, stop, seen) for k, v in x.items()
                           if k not in ('/Length', '/Filter', '/DecodeParms')}, data)
    if isinstance(x, pikepdf.Dictionary):
        return {str(k): canon(v, stop, seen) for k, v in x.items() if k != '/Parent'}
    if isinstance(x, pikepdf.Array):
        return [canon(v, stop, seen) for v in x]
    return repr(x)


# ------------------------------------------------------------------------------------------
# A: document-level identity
# ------------------------------------------------------------------------------------------

def check_document(ps, po, problems, info):
    if len(ps.pages) != 1 or len(po.pages) != 1:
        raise Unsupported(f'expected single-page files, got {len(ps.pages)} and {len(po.pages)} pages')
    a, b = ps.pages[0].obj, po.pages[0].obj
    sa, sb = {a.objgen}, {b.objgen}

    def catalog(p, stop):
        return canon(pikepdf.Dictionary({k: v for k, v in p.Root.items() if k != '/Pages'}), stop)
    if catalog(ps, sa) != catalog(po, sb):
        problems.append('document catalog differs (layers panel, metadata or viewer settings)')
    info_s, info_o = ps.trailer.get('/Info'), po.trailer.get('/Info')
    if (info_s is None) != (info_o is None) or (info_s is not None and canon(info_s) != canon(info_o)):
        problems.append('document info (title, author, dates) differs')
    for k in sorted(set(a.keys()) | set(b.keys())):
        if k in ('/Contents', '/Resources', '/Parent'):
            continue
        if k not in a or k not in b or canon(a[k], sa) != canon(b[k], sb):
            problems.append(f'page entry {k} differs (page boxes, georeferencing, annotations, ...)')
    ra, rb = ps.pages[0].Resources, po.pages[0].Resources
    for k in sorted(set(ra.keys()) | set(rb.keys())):
        if k == '/XObject':
            continue
        if k not in ra or k not in rb or canon(ra[k]) != canon(rb[k]):
            problems.append(f'page resource {k} differs (fonts, layer references or colour spaces)')
    xa, xb = ra.get('/XObject', pikepdf.Dictionary()), rb.get('/XObject', pikepdf.Dictionary())
    for k in xa.keys():
        if k not in xb or canon(xa[k]) != canon(xb[k]):
            problems.append(f'source XObject {k} missing or changed')
    extra = [k for k in xb.keys() if k not in xa]
    bad = [k for k in extra if xb[k].get('/Subtype') != '/Form']
    if bad:
        problems.append(f'new non-form XObjects added: {bad[:5]}')
    info['new_form_xobjects'] = len(extra)
    info['layers'] = len(ps.Root.OCProperties.OCGs) if '/OCProperties' in ps.Root else 0


def check_text_mupdf(path_s, path_o, problems):
    import pymupdf
    flags = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES
    a = pymupdf.open(path_s)[0].get_text('rawdict', flags=flags)
    b = pymupdf.open(path_o)[0].get_text('rawdict', flags=flags)
    if a != b:
        problems.append('MuPDF character-level text extraction differs')
    return sum(len(s['chars']) for bl in a['blocks'] if bl.get('type') == 0 for ln in bl['lines'] for s in ln['spans'])


# ------------------------------------------------------------------------------------------
# B-G: content interpretation
# ------------------------------------------------------------------------------------------

class Tables:
    """Interning shared by both files so ids are comparable between them."""

    def __init__(self):
        self.ids, self.items = {}, []
        self.clips = {}  # clip digest -> (evenodd, subpaths)

    def id(self, key):
        i = self.ids.get(key)
        if i is None:
            i = self.ids[key] = len(self.items)
            self.items.append(key)
        return i


class Records:
    def __init__(self):
        self.dig = []
        self.kind, self.style, self.clip, self.layer = array('b'), array('i'), array('i'), array('i')
        self.box = array('d')    # painted bbox incl. stroke reach + AA (4 per record)
        self.seg = array('d')    # straight stroke segments: x0 y0 x1 y1 reach (5 per record), else nan
        self.formbad = []        # (record index, painted box, form bbox) for check F

    def __len__(self):
        return len(self.kind)


class Interp:
    def __init__(self, pdf, tables):
        self.pdf, self.T = pdf, tables
        self.R = Records()
        self.text_ops = []
        ocp = pdf.Root.get('/OCProperties')
        self.ocg_index = {o.objgen: i for i, o in enumerate(ocp.OCGs)} if ocp is not None else {}
        self.font_ids, self.image_ids, self.cs_ids = {}, {}, {}

    @staticmethod
    def lookup(resources, category, name):
        d = resources.get(category)
        if d is None or name not in d:
            raise Unsupported(f'{category} {name} is not defined in the resources of the stream using it')
        return d[name]

    def layer_entry(self, tag, operand, resources):
        if tag == '/OC':
            obj = self.lookup(resources, '/Properties', operand) if isinstance(operand, pikepdf.Name) else operand
            if obj.get('/Type') == '/OCG':
                if obj.objgen not in self.ocg_index:
                    raise Unsupported('marked content refers to a layer not listed in OCProperties')
                return ('OCG', self.ocg_index[obj.objgen])
            if obj.get('/Type') == '/OCMD':
                ocgs = obj.get('/OCGs')
                ocgs = [ocgs] if isinstance(ocgs, pikepdf.Dictionary) else list(ocgs or [])
                return ('OCMD', tuple(self.ocg_index[o.objgen] for o in ocgs), repr(obj.get('/P')), repr(obj.get('/VE')))
            raise Unsupported('unknown optional-content object')
        return ('tag', tag, repr(canon(operand)) if operand is not None else None)

    def cached_digest(self, cache, obj):
        if not obj.is_indirect:
            return digest(canon(obj))
        if obj.objgen not in cache:
            cache[obj.objgen] = digest(canon(obj))
        return cache[obj.objgen]

    def font_id(self, resources, name):
        return (str(name), self.cached_digest(self.font_ids, self.lookup(resources, '/Font', name)))

    def colour_space(self, resources, name):
        name = str(name)
        if name in ('/DeviceCMYK', '/DeviceRGB', '/DeviceGray'):
            return name
        cs = self.lookup(resources, '/ColorSpace', pikepdf.Name(name))
        if str(cs) == '/Pattern' or (isinstance(cs, pikepdf.Array) and str(cs[0]) == '/Pattern'):
            raise Unsupported('pattern colour space')
        return self.cached_digest(self.cs_ids, cs)

    def run(self):
        page = self.pdf.pages[0]
        self.g = dict(ctm=IDENT, fill=('/DeviceGray', (0.0,)), stroke=('/DeviceGray', (0.0,)), fcs='/DeviceGray',
                      scs='/DeviceGray', w=1.0, J=0, j=0, M=10.0, dash=((), 0.0), ri=None, flat=None,
                      clips=(), clipbox=(-math.inf, -math.inf, math.inf, math.inf), clip_id=self.T.id(('clips', ())),
                      formbox=None, Tc=0.0, Tw=0.0, Tz=100.0, TL=0.0, font=None, fs=None, Tr=0, Ts=0.0)
        self.stack, self.layers = [], []
        self.layer_id = self.T.id(('layers', ()))
        self.exec(page.obj, page.Resources, depth=0)
        if self.layers:
            raise Unsupported('unbalanced marked content at the end of the page')
        return self

    def exec(self, stream, resources, depth):
        if depth > 8:
            raise Unsupported('Form XObjects nested too deeply')
        g, T, stack = self.g, self.T, self.stack
        path, cur, pending_clip = [], None, None
        tm = tlm = IDENT
        base = len(stack)
        for operands, op in pikepdf.parse_content_stream(stream):
            op = str(op)
            if op == 'c':
                v = tuple(float(x) for x in operands)
                cur.append(('c', v if g['ctm'] == IDENT else self.tp(g['ctm'], v)))
            elif op == 'l':
                v = (float(operands[0]), float(operands[1]))
                cur.append(('l', v if g['ctm'] == IDENT else self.tp(g['ctm'], v)))
            elif op == 'm':
                v = (float(operands[0]), float(operands[1]))
                cur = [('m', v if g['ctm'] == IDENT else self.tp(g['ctm'], v))]
                path.append(cur)
            elif op == 'q':
                stack.append(g)
                g = self.g = dict(g)
            elif op == 'Q':
                if len(stack) <= base:
                    raise Unsupported('Q without matching q')
                g = self.g = stack.pop()
            elif op == 'cm':
                g['ctm'] = mul(tuple(float(x) for x in operands), g['ctm'])
            elif op == 'h':
                if cur is not None:
                    cur.append(('h', ()))
            elif op in PAINT_OPS:
                if op != 'n' and path:
                    self.paint(op, path)
                if pending_clip:  # the new clip applies after this painting operation
                    self.add_clip(pending_clip, path)
                    pending_clip = None
                path, cur = [], None
            elif op in ('v', 'y'):
                v = tuple(float(x) for x in operands)
                pts = v if g['ctm'] == IDENT else self.tp(g['ctm'], v)
                p0 = cur[0][1] if cur[-1][0] == 'h' else cur[-1][1][-2:]
                cur.append(('c', p0 + pts if op == 'v' else pts + pts[2:4]))
            elif op == 're':
                x, y, w, h = (float(t) for t in operands)
                pts = (x, y, x + w, y, x + w, y + h, x, y + h)
                if g['ctm'] != IDENT:
                    pts = self.tp(g['ctm'], pts)
                path.append([('m', pts[0:2]), ('l', pts[2:4]), ('l', pts[4:6]), ('l', pts[6:8]), ('h', ())])
                cur = None
            elif op in ('W', 'W*'):
                pending_clip = op
            elif op == 'k':
                g['fill'], g['fcs'] = ('/DeviceCMYK', tuple(float(x) for x in operands)), '/DeviceCMYK'
            elif op == 'K':
                g['stroke'], g['scs'] = ('/DeviceCMYK', tuple(float(x) for x in operands)), '/DeviceCMYK'
            elif op == 'w':
                g['w'] = float(operands[0])
            elif op == 'J':
                g['J'] = int(operands[0])
            elif op == 'j':
                g['j'] = int(operands[0])
            elif op == 'M':
                g['M'] = float(operands[0])
            elif op == 'd':
                g['dash'] = (tuple(float(x) for x in operands[0]), float(operands[1]))
            elif op in ('BDC', 'BMC'):
                self.layers.append(self.layer_entry(str(operands[0]), operands[1] if op == 'BDC' else None, resources))
                self.layer_id = T.id(('layers', tuple(self.layers)))
            elif op == 'EMC':
                if not self.layers:
                    raise Unsupported('EMC without BDC/BMC')
                self.layers.pop()
                self.layer_id = T.id(('layers', tuple(self.layers)))
            elif op == 'BT':
                tm = tlm = IDENT
            elif op == 'ET':
                pass
            elif op == 'Tf':
                g['font'], g['fs'] = self.font_id(resources, operands[0]), float(operands[1])
            elif op == 'Tm':
                tm = tlm = tuple(float(x) for x in operands)
            elif op in ('Td', 'TD'):
                tx, ty = float(operands[0]), float(operands[1])
                if op == 'TD':
                    g['TL'] = -ty
                tm = tlm = mul((1.0, 0.0, 0.0, 1.0, tx, ty), tlm)
            elif op == 'T*':
                tm = tlm = mul((1.0, 0.0, 0.0, 1.0, 0.0, -g['TL']), tlm)
            elif op in ('Tc', 'Tw', 'Tz', 'TL', 'Ts'):
                g[op] = float(operands[0])
            elif op == 'Tr':
                g['Tr'] = int(operands[0])
                if g['Tr'] >= 4:
                    raise Unsupported('text rendering mode that adds text to the clip path')
            elif op in TEXT_SHOW:
                if op in ("'", '"'):
                    if op == '"':
                        g['Tw'], g['Tc'] = float(operands[0]), float(operands[1])
                    tm = tlm = mul((1.0, 0.0, 0.0, 1.0, 0.0, -g['TL']), tlm)
                self.text(op, operands, tm)
            elif op == 'Do':
                self.do(operands[0], resources, depth)
            elif op in ('g', 'G', 'rg', 'RG'):
                space = '/DeviceGray' if op in ('g', 'G') else '/DeviceRGB'
                val = (space, tuple(float(x) for x in operands))
                if op.islower():
                    g['fill'], g['fcs'] = val, space
                else:
                    g['stroke'], g['scs'] = val, space
            elif op in ('cs', 'CS'):
                cs = self.colour_space(resources, operands[0])
                if op == 'cs':
                    g['fcs'], g['fill'] = cs, (cs, None)
                else:
                    g['scs'], g['stroke'] = cs, (cs, None)
            elif op in ('sc', 'SC', 'scn', 'SCN'):
                if any(isinstance(x, pikepdf.Name) for x in operands):
                    raise Unsupported('pattern colour')
                val = tuple(float(x) for x in operands)
                if op.islower():
                    g['fill'] = (g['fcs'], val)
                else:
                    g['stroke'] = (g['scs'], val)
            elif op == 'ri':
                g['ri'] = str(operands[0])
            elif op == 'i':
                g['flat'] = float(operands[0])
            elif op in ('MP', 'DP'):
                pass  # marked-content points paint nothing
            else:  # gs, sh, inline images, BX/EX, Type 3 glyph operators, unknown operators
                raise Unsupported(f'operator {op} is not modelled by this checker')
        if len(stack) != base:
            raise Unsupported('unbalanced q/Q inside a content stream')

    @staticmethod
    def tp(m, v):
        out = []
        for i in range(0, len(v), 2):
            x, y = v[i], v[i + 1]
            out.append(m[0] * x + m[2] * y + m[4])
            out.append(m[1] * x + m[3] * y + m[5])
        return tuple(out)

    def add_clip(self, rule, path):
        g = self.g
        geom = tuple(tuple((op, tuple(round(v, 6) for v in pts)) for op, pts in sp) for sp in path)
        d = digest((rule, geom))
        if d not in self.T.clips:
            self.T.clips[d] = (rule == 'W*', path)
        g['clips'] = g['clips'] + (d,)
        g['clip_id'] = self.T.id(('clips', g['clips']))
        xs = [v for sp in path for _, pts in sp for v in pts[0::2]]
        ys = [v for sp in path for _, pts in sp for v in pts[1::2]]
        cb = g['clipbox']
        if xs:
            g['clipbox'] = (max(cb[0], min(xs)), max(cb[1], min(ys)), min(cb[2], max(xs)), min(cb[3], max(ys)))
        else:
            g['clipbox'] = (0.0, 0.0, -1.0, -1.0)  # empty clip: nothing below can paint

    def style_key(self, op):
        g = self.g
        return ('paint', op, g['fill'] if op in FILL_OPS else None,
                (g['stroke'], g['w'], g['J'], g['j'], g['M'], g['dash']) if op in STROKE_OPS else None,
                g['ri'], g['flat'])

    def stroke_segments(self, path):
        """Segments of a stroke-only path whose painted area is exactly the union of its segment
        strokes, or None. True for round joins + round caps without dashes; for any caps when the
        path is one open 2-point line (no joins at all)."""
        g = self.g
        round_both = g['j'] == 1 and g['J'] == 1 and not g['dash'][0]
        segs = []
        for sp in path:
            ops = [o for o, _ in sp]
            if any(o not in ('m', 'l', 'h') for o in ops) or 'h' in ops[:-1]:
                return None
            pts = [p for o, p in sp if o != 'h']
            closed = ops[-1] == 'h'
            if closed and pts[-1] != pts[0]:
                pts.append(pts[0])
            if round_both:
                if len(pts) == 1:
                    if not closed:
                        return None  # lone moveto: viewers differ on whether it paints
                    segs.append((pts[0], pts[0]))
                segs.extend(zip(pts, pts[1:]))
            elif len(path) == 1 and len(pts) == 2 and not closed and not g['dash'][0]:
                segs.append((pts[0], pts[1]))
            else:
                return None
        return segs

    def paint(self, op, path):
        g = self.g
        if op in ('s', 'b', 'b*'):
            path[-1] = path[-1] + [('h', ())]
            op = {'s': 'S', 'b': 'B', 'b*': 'B*'}[op]
        sid = self.T.id(self.style_key(op))
        cid, lid = g['clip_id'], self.layer_id
        stroke = op in STROKE_OPS
        weff = max(g['w'], 1.0)  # zero-width lines paint one device pixel: treat as >= 1 pt
        segs = self.stroke_segments(path) if op == 'S' else None
        if segs is not None:
            reach = weff / 2 * SQRT2 + AA
            for a, b in segs:
                a, b = (a, b) if a <= b else (b, a)
                self.add(K_SEG, sid, cid, lid, (a[0], a[1], b[0], b[1]),
                         min(a[0], b[0]) - reach, min(a[1], b[1]) - reach, max(a[0], b[0]) + reach, max(a[1], b[1]) + reach,
                         seg=(a[0], a[1], b[0], b[1], reach))
            return
        geom = tuple(tuple((o, tuple(round(v, 6) for v in p)) for o, p in sp) for sp in path)
        xs = [v for sp in path for _, p in sp for v in p[0::2]]
        ys = [v for sp in path for _, p in sp for v in p[1::2]]
        reach = (weff / 2 * max(g['M'], SQRT2) if stroke else 0.0) + AA
        self.add(K_PATH, sid, cid, lid, geom, min(xs) - reach, min(ys) - reach, max(xs) + reach, max(ys) + reach)

    def add(self, kind, sid, cid, lid, geom, x0, y0, x1, y1, seg=None):
        R = self.R
        if kind == K_SEG:
            geom = tuple(round(v, 6) for v in geom)
        R.dig.append(digest((kind, sid, cid, lid, geom)))
        R.kind.append(kind)
        R.style.append(sid)
        R.clip.append(cid)
        R.layer.append(lid)
        R.box.extend((x0, y0, x1, y1))
        R.seg.extend(seg if seg is not None else (math.nan,) * 5)
        fb = self.g['formbox']
        if fb is not None:
            cb = self.g['clipbox']
            px0, py0, px1, py1 = max(x0, cb[0]), max(y0, cb[1]), min(x1, cb[2]), min(y1, cb[3])
            if px0 < px1 and py0 < py1 and (px0 < fb[0] or py0 < fb[1] or px1 > fb[2] or py1 > fb[3]):
                R.formbad.append((len(R.kind) - 1, (px0, py0, px1, py1), fb))

    def text(self, op, operands, tm):
        g = self.g
        if g['font'] is None:
            raise Unsupported('text shown without a font')
        m = mul(tm, g['ctm'])
        state = ('text', op, g['font'], g['fs'], g['Tc'], g['Tw'], g['Tz'], g['TL'], g['Tr'], g['Ts'],
                 g['fill'], g['stroke'] if g['Tr'] in (1, 2, 5, 6) else None, g['ri'])

        def val(x):
            if isinstance(x, pikepdf.String):
                return bytes(x)
            if isinstance(x, pikepdf.Array):
                return [val(y) for y in x]
            return float(x)
        payload = repr([val(x) for x in operands])
        geo = tuple(round(v, 6) for v in m)
        sid = self.T.id(state)
        self.text_ops.append((sid, g['clip_id'], self.layer_id, geo, payload))
        scale = math.hypot(m[0], m[1]) + math.hypot(m[2], m[3])
        r = abs(g['fs'] or 0) * scale * (len(payload) + 2) + AA
        self.add(K_TEXT, sid, g['clip_id'], self.layer_id, (geo, payload), m[4] - r, m[5] - r, m[4] + r, m[5] + r)

    def do(self, name, resources, depth):
        xo = self.lookup(resources, '/XObject', name)
        g = self.g
        if xo.get('/Subtype') == '/Image':
            m = g['ctm']
            xs = [m[4], m[0] + m[4], m[2] + m[4], m[0] + m[2] + m[4]]
            ys = [m[5], m[1] + m[5], m[3] + m[5], m[1] + m[3] + m[5]]
            sid = self.T.id(('image', self.cached_digest(self.image_ids, xo), g['ri']))
            self.add(K_IMAGE, sid, g['clip_id'], self.layer_id, tuple(round(v, 6) for v in m),
                     min(xs) - AA, min(ys) - AA, max(xs) + AA, max(ys) + AA)
            return
        if xo.get('/Subtype') != '/Form':
            raise Unsupported(f'XObject subtype {xo.get("/Subtype")}')
        if '/Group' in xo or '/OC' in xo:
            raise Unsupported('form with a transparency group or optional content')
        self.stack.append(g)
        g = self.g = dict(g)
        g['ctm'] = mul(tuple(float(v) for v in xo.get('/Matrix', [1, 0, 0, 1, 0, 0])), g['ctm'])
        m = g['ctm']
        if abs(m[1]) > 1e-12 or abs(m[2]) > 1e-12:
            raise Unsupported('rotated or skewed form placement')
        bb = [float(v) for v in xo.BBox]
        xa, xb = m[0] * bb[0] + m[4], m[0] * bb[2] + m[4]
        ya, yb = m[3] * bb[1] + m[5], m[3] * bb[3] + m[5]
        fb = (min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb))
        if g['formbox'] is not None:
            o = g['formbox']
            fb = (max(fb[0], o[0]), max(fb[1], o[1]), min(fb[2], o[2]), min(fb[3], o[3]))
        g['formbox'] = fb
        res = xo.get('/Resources')
        self.exec(xo, resources if res is None else res, depth + 1)  # no /Resources: inherited
        self.g = self.stack.pop()


# ------------------------------------------------------------------------------------------
# D: exact "hidden by its clip" prover
# ------------------------------------------------------------------------------------------

def seg_seg_dist(a, b):
    """Euclidean distance between segments a[i] = (x0, y0, x1, y1) and b[i]."""
    p0, p1, q0, q1 = a[:, 0:2], a[:, 2:4], b[:, 0:2], b[:, 2:4]

    def pt_seg(p, s0, s1):
        d = s1 - s0
        L2 = (d * d).sum(1)
        t = np.clip(np.where(L2 > 0, ((p - s0) * d).sum(1) / np.where(L2 > 0, L2, 1), 0.0), 0, 1)
        return np.hypot(*(p - (s0 + t[:, None] * d)).T)

    def orient(o, u, v):
        return (u[:, 0] - o[:, 0]) * (v[:, 1] - o[:, 1]) - (u[:, 1] - o[:, 1]) * (v[:, 0] - o[:, 0])

    cross = (orient(p0, p1, q0) * orient(p0, p1, q1) < 0) & (orient(q0, q1, p0) * orient(q0, q1, p1) < 0)
    d = np.minimum(np.minimum(pt_seg(p0, q0, q1), pt_seg(p1, q0, q1)), np.minimum(pt_seg(q0, p0, p1), pt_seg(q1, p0, p1)))
    return np.where(cross, 0.0, d)


def cell_key(i, j):
    return (i + (1 << 24)) * (1 << 25) + (j + (1 << 24))


def expand_cells(i0, i1, j0, j1):
    """All cells of the integer boxes [i0..i1] x [j0..j1]: (cell keys, box ids)."""
    ni, nj = i1 - i0 + 1, j1 - j0 + 1
    n = ni * nj
    ids = np.repeat(np.arange(len(i0)), n)
    k = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
    nj_r = np.repeat(nj, n)
    return cell_key(np.repeat(i0, n) + k // nj_r, np.repeat(j0, n) + k % nj_r), ids


def capsule_cells(q, C):
    """Cells within reach of each capsule (ax, ay, bx, by, r), by sampling its axis every C/4."""
    ax, ay, bx, by, r = q.T
    ns = np.ceil(np.hypot(bx - ax, by - ay) / (C / 4)).astype(np.int64) + 1
    qi = np.repeat(np.arange(len(q)), ns)
    k = np.arange(ns.sum()) - np.repeat(np.cumsum(ns) - ns, ns)
    nsr = np.repeat(ns, ns)
    t = np.where(nsr > 1, k / np.maximum(nsr - 1, 1), 0.0)
    sx = np.repeat(ax, ns) + t * np.repeat(bx - ax, ns)
    sy = np.repeat(ay, ns) + t * np.repeat(by - ay, ns)
    R = np.repeat(r, ns) + C / 4  # every axis point is within C/8 of a sample
    keys, sid = expand_cells(np.floor((sx - R) / C).astype(np.int64), np.floor((sx + R) / C).astype(np.int64),
                             np.floor((sy - R) / C).astype(np.int64), np.floor((sy + R) / C).astype(np.int64))
    return np.unique(np.stack([qi[sid], keys], 1), axis=0)


class ClipIndex:
    """Edges of one clip path with a uniform-grid index.

    Proximity edges bound the clip boundary (straight edges; for curves, all six segments
    between the four control points, which contain the curve's convex hull boundary). Winding
    edges define the region (curves replaced by their chords, valid for points outside the
    control hull, which `touching` guarantees by refusing points inside any curve's control box).
    """
    CELL = 8.0

    def __init__(self, evenodd, path):
        self.evenodd = evenodd
        prox, wind, hulls = [], [], []
        for sp in path:
            start = cur = None
            for op, p in sp:
                if op == 'm':
                    start = cur = p[0:2]
                elif op == 'l':
                    prox.append(cur + p[0:2])
                    wind.append(cur + p[0:2])
                    cur = p[0:2]
                elif op == 'c':
                    q = [cur, p[0:2], p[2:4], p[4:6]]
                    prox.extend(q[i] + q[j] for i in range(4) for j in range(i + 1, 4))
                    hulls.append((min(t[0] for t in q), min(t[1] for t in q), max(t[0] for t in q), max(t[1] for t in q)))
                    wind.append(cur + p[4:6])
                    cur = p[4:6]
                elif op == 'h':
                    if cur != start:
                        prox.append(cur + start)
                        wind.append(cur + start)
                    cur = start
            if cur is not None and cur != start:  # clip paths are implicitly closed
                prox.append(cur + start)
                wind.append(cur + start)
        self.prox = np.array(prox, float).reshape(-1, 4)
        self.wind = np.array(wind, float).reshape(-1, 4)
        self.hulls = np.array(hulls, float).reshape(-1, 4)
        C, e = self.CELL, self.prox
        keys, ids = expand_cells(np.floor(np.minimum(e[:, 0], e[:, 2]) / C).astype(np.int64),
                                 np.floor(np.maximum(e[:, 0], e[:, 2]) / C).astype(np.int64),
                                 np.floor(np.minimum(e[:, 1], e[:, 3]) / C).astype(np.int64),
                                 np.floor(np.maximum(e[:, 1], e[:, 3]) / C).astype(np.int64))
        o = np.argsort(keys, kind='stable')
        self.gkeys, self.gids = keys[o], ids[o]
        w = self.wind
        r0 = np.floor(np.minimum(w[:, 1], w[:, 3]) / C).astype(np.int64)
        n = np.floor(np.maximum(w[:, 1], w[:, 3]) / C).astype(np.int64) - r0 + 1
        rows = np.repeat(r0, n) + (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n))
        rid = np.repeat(np.arange(len(w)), n)
        o = np.argsort(rows, kind='stable')
        self.rkeys, self.rids = rows[o], rid[o]

    def touching(self, q):
        """q: (n, 5) capsules (ax, ay, bx, by, r). True where a clip edge comes within r."""
        res = np.zeros(len(q), bool)
        if len(q) and len(self.prox):
            pair = capsule_cells(q, self.CELL)
            lo = np.searchsorted(self.gkeys, pair[:, 1], 'left')
            cnt = np.searchsorted(self.gkeys, pair[:, 1], 'right') - lo
            keep = cnt > 0
            pair, lo, cnt = pair[keep], lo[keep], cnt[keep]
            csum = np.cumsum(cnt)
            start = 0
            while start < len(pair):  # bounded memory: ~2M query/edge pairs per step
                stop = int(np.searchsorted(csum, (csum[start - 1] if start else 0) + 2_000_000, 'right'))
                stop = max(stop, start + 1)
                c = cnt[start:stop]
                qq = np.repeat(pair[start:stop, 0], c)
                k = np.arange(c.sum()) - np.repeat(np.cumsum(c) - c, c)
                ee = self.gids[np.repeat(lo[start:stop], c) + k]
                hit = seg_seg_dist(q[qq, :4], self.prox[ee]) <= q[qq, 4]
                res[qq[hit]] = True
                start = stop
        for h in self.hulls:  # capsule start inside a curve's control box: cannot decide, refuse
            res |= (q[:, 0] >= h[0]) & (q[:, 0] <= h[2]) & (q[:, 1] >= h[1]) & (q[:, 1] <= h[3])
        return res

    def inside(self, px, py):
        """Clip-region membership of points by ray casting (even-odd or nonzero winding)."""
        res = np.zeros(len(px), bool)
        if len(self.wind) == 0:
            return res
        rows = np.floor(py / self.CELL).astype(np.int64)
        order = np.argsort(rows, kind='stable')
        urows, starts = np.unique(rows[order], return_index=True)
        ends = np.append(starts[1:], len(px))
        for row, s, e in zip(urows, starts, ends):
            lo, hi = np.searchsorted(self.rkeys, row, 'left'), np.searchsorted(self.rkeys, row, 'right')
            if hi == lo:
                continue
            E = self.wind[self.rids[lo:hi]]
            x0, y0, x1, y1 = E[:, 0], E[:, 1], E[:, 2], E[:, 3]
            idx = order[s:e]
            for c in range(0, len(idx), 2048):
                ii = idx[c:c + 2048]
                X, Y = px[ii][:, None], py[ii][:, None]
                up, down = (y0 <= Y) & (y1 > Y), (y1 <= Y) & (y0 > Y)
                with np.errstate(divide='ignore', invalid='ignore'):
                    right = x0 + (Y - y0) * (x1 - x0) / (y1 - y0) > X
                if self.evenodd:
                    res[ii] = ((up | down) & right).sum(1) % 2 == 1
                else:
                    res[ii] = ((up & right).sum(1) - (down & right).sum(1)) != 0
        return res


class Prover:
    def __init__(self, tables):
        self.T, self.idx = tables, {}

    def hidden(self, caps, clip_ids):
        """True where the capsule provably misses the region of some clip in its clip stack."""
        out = np.zeros(len(caps), bool)
        groups = defaultdict(list)
        for i, c in enumerate(clip_ids):
            groups[int(c)].append(i)
        for cid, members in groups.items():
            todo = np.array(members)
            for d in reversed(self.T.items[cid][1]):  # innermost clip first
                if len(todo) == 0:
                    break
                if d not in self.idx:
                    self.idx[d] = ClipIndex(*self.T.clips[d])
                ci = self.idx[d]
                q = caps[todo]
                ok = ~ci.touching(q)
                ok[ok] = ~ci.inside(q[ok, 0], q[ok, 1])
                out[todo[ok]] = True
                todo = todo[~ok]
        return out

    def explain(self, cap, clip_id):
        """Why a capsule is not provably hidden: per clip, nearest edge distance and inside flag."""
        parts = []
        for d in reversed(self.T.items[clip_id][1]):
            if d not in self.idx:
                self.idx[d] = ClipIndex(*self.T.clips[d])
            ci = self.idx[d]
            dist = seg_seg_dist(np.repeat(cap[None, :4], len(ci.prox), 0), ci.prox).min() if len(ci.prox) else math.inf
            ins = bool(ci.inside(cap[None, 0], cap[None, 1])[0])
            parts.append(f'clip with {len(ci.prox)} edges: nearest edge {dist:.2f} pt (needs > {cap[4]:.2f}), '
                         f'{"inside" if ins else "outside"} its region')
        return '; '.join(parts) or 'no clip'


def capsules_for(R, idx):
    """A capsule containing each record's painted area (stroke reach and AA included)."""
    box = np.frombuffer(R.box, float).reshape(-1, 4)[idx]
    seg = np.frombuffer(R.seg, float).reshape(-1, 5)[idx]
    caps = np.empty((len(idx), 5))
    isseg = ~np.isnan(seg[:, 0])
    caps[isseg] = seg[isseg]
    b = box[~isseg]
    w, h = b[:, 2] - b[:, 0], b[:, 3] - b[:, 1]
    cx, cy = (b[:, 0] + b[:, 2]) / 2, (b[:, 1] + b[:, 3]) / 2
    # a w x h rectangle (w >= h) lies within h/2 * sqrt(2) of its centre line shortened by h/2
    caps[~isseg] = np.where((w >= h)[:, None],
                            np.stack([b[:, 0] + h / 2, cy, b[:, 2] - h / 2, cy, h / 2 * SQRT2], 1),
                            np.stack([cx, b[:, 1] + w / 2, cx, b[:, 3] - w / 2, w / 2 * SQRT2], 1))
    return caps


# ------------------------------------------------------------------------------------------
# comparison
# ------------------------------------------------------------------------------------------

def commutative(item):
    """Can records of this style be painted in any order with an identical result?"""
    if item[0] == 'paint':
        fill, stroke = item[2], item[3]
        return fill is None or stroke is None or fill == stroke[0]
    if item[0] == 'text':
        return item[8] == 0  # fill-only text paints one opaque colour
    return False


def order_violations(orig_idx, boxes):
    """Overlapping pairs whose relative order differs (inputs in optimized paint order)."""
    G, cells, viol = 16.0, defaultdict(list), 0
    for k in range(len(orig_idx)):
        x0, y0, x1, y1 = boxes[k]
        seen = set()
        for gx in range(int(x0 // G), int(x1 // G) + 1):
            for gy in range(int(y0 // G), int(y1 // G) + 1):
                for j in cells[(gx, gy)]:
                    if j not in seen:
                        seen.add(j)
                        b = boxes[j]
                        if b[0] < x1 and x0 < b[2] and b[1] < y1 and y0 < b[3] and orig_idx[j] > orig_idx[k]:
                            viol += 1
                cells[(gx, gy)].append(k)
    return viol


def find_owners(segS, candS, keyS, segO, pieces, keyO):
    """For each optimized piece, the candidate source segments it lies on."""
    C = 16.0
    if len(candS) == 0 or len(pieces) == 0:
        return {}
    caps = np.concatenate([segS[candS, :4], np.full((len(candS), 1), TOL_LINE)], 1)
    pair = capsule_cells(caps, C)
    o = np.argsort(pair[:, 1], kind='stable')
    gk, gi = pair[o, 1], candS[pair[o, 0]]
    mid = (segO[pieces, 0:2] + segO[pieces, 2:4]) / 2
    pk = cell_key(np.floor(mid[:, 0] / C).astype(np.int64), np.floor(mid[:, 1] / C).astype(np.int64))
    lo, hi = np.searchsorted(gk, pk, 'left'), np.searchsorted(gk, pk, 'right')
    owners = {}
    for n, p in enumerate(pieces):
        cand = gi[lo[n]:hi[n]]
        cand = cand[keyS[cand] == keyO[p]]
        if len(cand) == 0:
            continue
        a = segS[cand, :4]
        d = a[:, 2:4] - a[:, 0:2]
        L = np.hypot(d[:, 0], d[:, 1])
        ok = L > 0
        u = d / np.where(L > 0, L, 1)[:, None]
        for px, py in (segO[p, 0:2], segO[p, 2:4]):
            vx, vy = px - a[:, 0], py - a[:, 1]
            t = vx * u[:, 0] + vy * u[:, 1]
            ok &= (np.abs(vx * u[:, 1] - vy * u[:, 0]) <= TOL_LINE) & (t >= -TOL_LINE) & (t <= L + TOL_LINE)
        if ok.any():
            owners[p] = cand[ok]
    return owners


def compare(T, S, O, problems, info, layer_names):
    t0 = time.time()
    RS, RO = S.R, O.R
    nS, nO = len(RS), len(RO)
    kindS, kindO = np.frombuffer(RS.kind, np.int8), np.frombuffer(RO.kind, np.int8)
    styS, styO = np.frombuffer(RS.style, np.int32), np.frombuffer(RO.style, np.int32)
    clS, clO = np.frombuffer(RS.clip, np.int32), np.frombuffer(RO.clip, np.int32)
    lyS, lyO = np.frombuffer(RS.layer, np.int32), np.frombuffer(RO.layer, np.int32)
    segS, segO = np.frombuffer(RS.seg, float).reshape(-1, 5), np.frombuffer(RO.seg, float).reshape(-1, 5)
    if len(T.items) >= 1 << 20:
        raise Unsupported('too many distinct styles/clips/layers for the run check')
    keyS = (styS.astype(np.int64) << 40) | (clS.astype(np.int64) << 20) | lyS.astype(np.int64)
    keyO = (styO.astype(np.int64) << 40) | (clO.astype(np.int64) << 20) | lyO.astype(np.int64)

    def describe(R, i, kinds, sty, ly):
        b = np.frombuffer(R.box, float).reshape(-1, 4)[i]
        lay = [layer_names.get(e[1], str(e)) if e[0] == 'OCG' else str(e) for e in T.items[ly[i]][1]]
        return (f'{KIND_NAME[int(kinds[i])]} near x={0.5 * (b[0] + b[2]):.1f} y={0.5 * (b[1] + b[3]):.1f} pt '
                f'(PDF coordinates), layers {lay or "-"}, style {str(T.items[sty[i]])[:140]}')

    # exact matches, k-th identical record to k-th identical record
    pos = defaultdict(deque)
    for i, d in enumerate(RS.dig):
        pos[d].append(i)
    match = np.full(nO, -1, np.int64)
    for i, d in enumerate(RO.dig):
        q = pos.get(d)
        if q:
            match[i] = q.popleft()
    used = np.zeros(nS, bool)
    used[match[match >= 0]] = True
    unS, unO = np.nonzero(~used)[0], np.nonzero(match < 0)[0]
    info['records_source'], info['records_optimized'] = nS, nO
    info['records_identical'] = int((match >= 0).sum())

    # B: extra records must be trimmed pieces of source segments
    extra = unO[kindO[unO] != K_SEG]
    for i in extra[:5]:
        problems.append('EXTRA content that the source does not have: ' + describe(RO, i, kindO, styO, lyO))
    if len(extra):
        problems.append(f'{len(extra)} optimized records have no counterpart in the source')
    pieces = unO[kindO[unO] == K_SEG]
    candS = unS[kindS[unS] == K_SEG]
    owners = find_owners(segS, candS, keyS, segO, pieces, keyO)
    orphans = [p for p in pieces if p not in owners]
    for p in orphans[:5]:
        problems.append('EXTRA stroke segment that does not lie on any source segment: ' + describe(RO, p, kindO, styO, lyO))
    if orphans:
        problems.append(f'{len(orphans)} optimized stroke segments do not lie on a source segment')
    by_geom = defaultdict(list)
    for i in candS:
        by_geom[RS.dig[i]].append(i)
    pieces_of, ambiguous = defaultdict(list), 0
    for p, own in owners.items():
        geoms = {RS.dig[i] for i in own}
        if len(geoms) != 1:
            ambiguous += 1
            continue
        pieces_of[geoms.pop()].append(p)
    if ambiguous:
        problems.append(f'{ambiguous} trimmed pieces lie on several different source segments (cannot verify)')
    cut_caps, cut_clip, trimmed, max_dev = [], [], set(), 0.0
    mapped = match.copy()
    for g_dig, plist in pieces_of.items():
        srcs = sorted(by_geom[g_dig])
        by_piece = defaultdict(list)
        for p in sorted(plist):
            by_piece[RO.dig[p]].append(p)
        if any(len(v) != len(srcs) for v in by_piece.values()):
            problems.append(f'trimmed segment multiplicity mismatch: {len(srcs)} source copies, '
                            f'piece copies {sorted(len(v) for v in by_piece.values())[:5]}')
            continue
        for v in by_piece.values():  # k-th copy of each piece belongs to the k-th source copy
            for p, s in zip(v, srcs):
                mapped[p] = s
        a = segS[srcs[0], :4]
        d = a[2:4] - a[0:2]
        L = math.hypot(*d)
        u = d / L
        ivs = []
        for v in by_piece.values():
            p = v[0]
            ends = (segO[p, 0:2], segO[p, 2:4])
            ts = sorted((x - a[0]) * u[0] + (y - a[1]) * u[1] for x, y in ends)
            max_dev = max(max_dev, *(abs((x - a[0]) * u[1] - (y - a[1]) * u[0]) for x, y in ends))
            ivs.append((max(ts[0], 0.0), min(ts[1], L)))
        ivs.sort()
        if any(ivs[k][1] > ivs[k + 1][0] + TOL_LINE for k in range(len(ivs) - 1)):
            problems.append('trimmed pieces of one source segment overlap (would paint twice)')
            continue
        prev, reach = 0.0, segS[srcs[0], 4]
        gaps = []
        for s_, e_ in ivs:  # gaps below the 0.001 pt tolerance are rounding, not cuts
            if s_ - prev > TOL_LINE:
                gaps.append((prev, s_))
            prev = max(prev, e_)
        if L - prev > TOL_LINE:
            gaps.append((prev, L))
        for s_, e_ in gaps:
            cut_caps.append((a[0] + s_ * u[0], a[1] + s_ * u[1], a[0] + e_ * u[0], a[1] + e_ * u[1], reach))
            cut_clip.append(clS[srcs[0]])
        trimmed.update(srcs)
    info['trimmed_segments'] = len(trimmed)
    info['trimmed_pieces'] = int(len(pieces) - len(orphans))
    info['trim_max_deviation_pt'] = float(f'{max_dev:.2g}')

    # C/D: every other missing source record must be provably hidden by its clip
    missing = np.array([i for i in unS if i not in trimmed], np.int64)
    prover = Prover(T)
    t1 = time.time()
    hidden = prover.hidden(capsules_for(RS, missing), clS[missing]) if len(missing) else np.zeros(0, bool)
    cut_ok = prover.hidden(np.array(cut_caps).reshape(-1, 5), np.array(cut_clip, np.int64)) if cut_caps else np.zeros(0, bool)
    info['hidden_proof_seconds'] = round(time.time() - t1, 1)
    info['removed_hidden_records'] = int(hidden.sum())
    if len(missing) and not hidden.all():
        lost = missing[~hidden]
        lost_caps = capsules_for(RS, lost[:8])
        for k, i in enumerate(lost[:8]):
            problems.append('MISSING source content that is not provably hidden: ' + describe(RS, i, kindS, styS, lyS)
                            + ' | ' + prover.explain(lost_caps[k], clS[i]))
        problems.append(f'{len(lost)} source records are missing and could not be proven hidden')
    if len(cut_ok) and not cut_ok.all():
        caps = np.array(cut_caps)
        for k in np.nonzero(~cut_ok)[0][:8]:
            c = caps[k]
            problems.append(f'CUT-AWAY part of a trimmed stroke not provably hidden: from ({c[0]:.2f}, {c[1]:.2f}) to '
                            f'({c[2]:.2f}, {c[3]:.2f}) | ' + prover.explain(c, cut_clip[k]))
        problems.append(f'{int((~cut_ok).sum())} cut-away parts of trimmed strokes could not be proven hidden')
    by_layer = Counter()
    for i in missing[hidden] if len(missing) else []:
        names = [layer_names.get(e[1], '?') for e in T.items[lyS[i]][1] if e[0] == 'OCG']
        by_layer[names[-1] if names else '(no layer)'] += 1
    info['removed_hidden_by_layer'] = dict(by_layer.most_common())

    # E: paint order
    kept = np.ones(nS, bool)
    kept[missing] = False
    idxS = np.nonzero(kept)[0]
    ks = keyS[idxS]
    brkS = np.concatenate([[True], ks[1:] != ks[:-1]]) if len(ks) else np.zeros(0, bool)
    runS = np.full(nS, -1, np.int64)
    runS[idxS] = np.cumsum(brkS) - 1
    brkO = np.concatenate([[True], keyO[1:] != keyO[:-1]]) if nO else np.zeros(0, bool)
    runO = np.cumsum(brkO) - 1
    seqS, seqO = ks[brkS], keyO[brkO]
    info['paint_runs'] = int(len(seqS))
    if len(seqS) != len(seqO) or not np.array_equal(seqS, seqO):
        k = next((k for k in range(min(len(seqS), len(seqO))) if seqS[k] != seqO[k]), min(len(seqS), len(seqO)))
        problems.append(f'paint order differs at style/clip/layer run #{k} ({len(seqS)} runs in source, {len(seqO)} optimized)')
    else:
        ok_rec = mapped >= 0
        moved = ok_rec & (runS[np.maximum(mapped, 0)] != runO)
        if moved.any():
            problems.append(f'{int(moved.sum())} records moved to a different paint run (paint order changed)')
        nc = np.array([not commutative(T.items[s]) for s in range(len(T.items))])
        boxO = np.frombuffer(RO.box, float).reshape(-1, 4)
        nc_runs = np.unique(runO[nc[styO] & ok_rec]) if nO else []
        viol = 0
        for r in nc_runs:
            members = np.nonzero((runO == r) & ok_rec)[0]
            viol += order_violations(mapped[members], boxO[members])
        info['noncommutative_runs_checked'] = int(len(nc_runs))
        if viol:
            problems.append(f'{viol} overlapping shapes changed their relative paint order')

    # F: form bbox
    if RO.formbad:
        i, painted, fb = RO.formbad[0]
        problems.append(f'{len(RO.formbad)} shapes inside Form XObjects reach outside the form BBox and would be '
                        f'clipped, e.g. painted box {tuple(round(v, 1) for v in painted)} vs BBox {tuple(round(v, 1) for v in fb)}')
    # G: text sequence
    if S.text_ops != O.text_ops:
        problems.append('text-showing operations differ (content, font, position, colour, layer or order)')
    info['text_operations'] = len(S.text_ops)
    info['compare_seconds'] = round(time.time() - t0, 1)


# ------------------------------------------------------------------------------------------

def file_sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for c in iter(lambda: f.read(1 << 20), b''):
            h.update(c)
    return h.hexdigest()


RECORD_FIELDS = ('dig', 'kind', 'style', 'clip', 'layer', 'box', 'seg', 'formbad')


def interpret_source(path, cache_dir):
    """Interpret the source, or load it from a cache keyed by the source file AND this checker's
    own code, so a cached result can never outlive a change to the checker."""
    with open(__file__, 'rb') as f:
        code = hashlib.sha256(f.read()).hexdigest()[:16]
    cpath = os.path.join(cache_dir, f'source-{file_sha(path)}-{code}.pickle') if cache_dir else None
    if cpath and os.path.exists(cpath):
        with open(cpath, 'rb') as f:
            state = pickle.load(f)
        T = Tables()
        T.ids, T.items, T.clips = state['tables']
        S = Interp.__new__(Interp)
        S.R, S.text_ops = Records(), state['text_ops']
        for k in RECORD_FIELDS:
            setattr(S.R, k, state['records'][k])
        print('  source: interpretation loaded from cache', flush=True)
        return T, S
    T = Tables()
    t = time.time()
    S = Interp(pikepdf.open(path), T).run()
    print(f'  source: {len(S.R):,} records in {time.time() - t:.0f}s', flush=True)
    if cpath:
        os.makedirs(cache_dir, exist_ok=True)
        state = dict(tables=(T.ids, T.items, T.clips), text_ops=S.text_ops,
                     records={k: getattr(S.R, k) for k in RECORD_FIELDS})
        with open(cpath + '.tmp', 'wb') as f:
            pickle.dump(state, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(cpath + '.tmp', cpath)
    return T, S


def main():
    ap = argparse.ArgumentParser(description='Prove that an optimized chart PDF shows exactly what the source shows.')
    ap.add_argument('source')
    ap.add_argument('optimized')
    ap.add_argument('--cache', help="directory for caching the source's interpretation (keyed by SHA-256)")
    ap.add_argument('--json', help='also write the report as JSON to this path')
    a = ap.parse_args()
    t0 = time.time()
    problems, info = [], {}
    try:
        ps, po = pikepdf.open(a.source), pikepdf.open(a.optimized)
        print('A    document structure', flush=True)
        check_document(ps, po, problems, info)
        layer_names = {i: str(o.get('/Name')) for i, o in enumerate(ps.Root.OCProperties.OCGs)} \
            if '/OCProperties' in ps.Root else {}
        print('B-F  interpreting every drawing operation (a few minutes for a large chart)', flush=True)
        T, S = interpret_source(a.source, a.cache)
        t = time.time()
        O = Interp(po, T).run()
        print(f'  optimized: {len(O.R):,} records in {time.time() - t:.0f}s', flush=True)
        compare(T, S, O, problems, info, layer_names)
        print('G    text through MuPDF', flush=True)
        info['characters'] = check_text_mupdf(a.source, a.optimized, problems)
        status = 'FAIL' if problems else 'PASS'
    except Unsupported as e:
        status = 'UNSUPPORTED'
        problems.append(f'cannot verify: {e}')
    info['seconds'] = round(time.time() - t0, 1)
    print()
    for k, v in info.items():
        print(f'  {k}: {v}')
    print()
    for p in problems:
        print('  PROBLEM:', p)
    print('\nVERDICT:', {
        'PASS': 'PASS. The optimized chart paints exactly what the source paints.',
        'FAIL': 'FAIL. Do not use the optimized file.',
        'UNSUPPORTED': 'UNSUPPORTED. The checker cannot verify this file. Do not use the optimized file.'}[status])
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        with open(a.json, 'w') as f:
            json.dump(dict(status=status, problems=problems, info=info, source=a.source, optimized=a.optimized),
                      f, indent=1, default=str)
    sys.exit({'PASS': 0, 'FAIL': 1}.get(status, 2))


if __name__ == '__main__':
    main()
