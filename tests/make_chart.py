"""Build a small synthetic chart PDF with the structures an ArcGIS VFR chart export uses.

usage: make_chart.py out.pdf

- one page with a page-sized clip and two layers (optional content groups),
- a buffer band drawn ArcGIS-style: a grid of dots (B*) and diagonal hatch lines (S) over a large
  rectangle, masked by an even-odd clip path that is only a narrow ring,
- long road polylines with round joins and caps (long enough to be split),
- a zero-length round-capped stroke (paints a dot) and a butt-capped one,
- text labels with a standard font.
"""
import math
import sys
import zlib

import pikepdf
from pikepdf import Array, Dictionary, Name


def ring(cx, cy, r, n=64):
    return [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n)) for k in range(n)]


def path(points):
    s = f'{points[0][0]:.3f} {points[0][1]:.3f} m '
    s += ''.join(f'{x:.3f} {y:.3f} l ' for x, y in points[1:])
    return s + 'h '


def build(out):
    W, H = 600.0, 400.0
    ops = ['/OC /MC0 BDC']
    ops.append(f'q 0 {H} m 0 0 l {W} 0 l {W} {H} l h W* n')
    # roads: long round-joined polylines
    ops.append('q 0 0.4 0.8 0 K 1.44 w 1 j 1 J')
    for k in range(3):
        pts = [(20 + 9 * i, 60 + 40 * k + 15 * math.sin(i / 4)) for i in range(60)]
        X, Y = pts[0]
        rel = ' '.join(f'{x - X:.3f} {y - Y:.3f} l' for x, y in pts[1:])
        ops.append(f'q 1 0 0 1 {X:.4f} {Y:.4f} cm 0 0 m {rel} S Q')
    ops.append('q 1 0 0 1 300.5 300.25 cm 0 0 m 0 0 l S Q')  # zero-length, round caps: a dot
    ops.append('Q')
    ops.append('q 0 0 0 0.6 K 1.76 w 0 J 1 j q 1 0 0 1 310 300 cm 0 0 m 0 0 l S Q Q')  # zero-length, butt caps
    ops.append('EMC')
    # airspace buffer band (second layer): dots and hatch over a big rectangle, clipped to a ring
    ops.append('/OC /MC1 BDC')
    outer, inner = ring(400, 200, 120), ring(400, 200, 105)
    ops.append('q ' + path(outer) + path(inner[::-1]) + 'W* n')
    ops.append('0 0.9 0.2 0 k 0 0.9 0.2 0 K 0.3 w')
    for i in range(0, 64):
        for j in range(0, 64):
            x, y = 270 + 4.1 * i, 70 + 4.1 * j
            ops.append(f'q 1 0 0 1 {x:.4f} {y:.4f} cm 0 0 m 0.4 0 0.4 0.8 0 0.8 c -0.4 0.8 -0.4 0 0 0 c h B* Q')
    ops.append('0 0.9 0.2 0 K 0.5 w 0 J 0 j')
    for i in range(-40, 40):
        x = 400 + 6 * i
        ops.append(f'q 1 0 0 1 {x:.4f} 60 cm 0 0 m 280 280 l S Q')
    ops.append('Q')
    ops.append('EMC')
    # labels
    ops.append('/OC /MC0 BDC BT /F1 9 Tf 0 0 0 1 k 1 0 0 1 40 30 Tm (RIGA EVRA) Tj 0 12 Td (TUKUMS EVTA) Tj ET EMC')
    ops.append('Q')
    content = '\n'.join(ops).encode()

    pdf = pikepdf.new()
    ocgs = [pdf.make_indirect(Dictionary(Type=Name.OCG, Name=pikepdf.String(n))) for n in ('topography', 'AirspaceBuffer')]
    pdf.Root.OCProperties = Dictionary(OCGs=Array(ocgs), D=Dictionary(Order=Array(ocgs), ON=Array(ocgs)))
    font = Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica, Encoding=Name.WinAnsiEncoding)
    stream = pikepdf.Stream(pdf, zlib.compress(content, 9))
    stream.Filter = Name.FlateDecode
    page = Dictionary(Type=Name.Page, MediaBox=Array([0, 0, W, H]), CropBox=Array([0, 0, W, H]),
                      Resources=Dictionary(Font=Dictionary(F1=font), XObject=Dictionary(),
                                           Properties=Dictionary(MC0=ocgs[0], MC1=ocgs[1])),
                      Contents=pdf.make_indirect(stream))
    pdf.pages.append(pikepdf.Page(page))
    pdf.docinfo['/Title'] = 'Synthetic test chart'
    pdf.save(out)


if __name__ == '__main__':
    build(sys.argv[1])
