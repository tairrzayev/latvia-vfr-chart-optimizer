# How the optimizer and the checkers work

## Contents

- [Why these charts are slow](#why-these-charts-are-slow)
- [Optimizer stages](#optimizer-stages)
- [What the optimizer refuses](#what-the-optimizer-refuses)
- [verify_chart.py: the exact check](#verify_chartpy-the-exact-check)
- [render_compare.py: what real engines draw](#render_comparepy-what-real-engines-draw)
- [mutation_test.py: checking the checkers](#mutation_testpy-checking-the-checkers)
- [Limits](#limits)

## Why these charts are slow

ArcGIS/ArcMap exports a chart as one enormous page whose content stream draws every symbol as a
self-contained block:

```
q 1 0 0 1 X Y cm <path> <paint> Q
```

Patterned areas (for example the dotted and hatched buffer bands along airspace boundaries) are
exported as a full grid of dots and long hatch lines over a large rectangle, then masked by a clip
path that is only a narrow band. Viewers have to read, transform and clip every one of those
shapes on every redraw, even though most of them can never be painted. There is also no structure
that lets a viewer skip content that is off screen when zoomed in.

## Optimizer stages

Every stage rewrites drawing blocks only and copies every other byte verbatim. Layers (optional
content), text, images, fonts, metadata and georeferencing (`/VP`) are untouched; the final stage
inserts the new content into the source document's own object graph.

| Stage | What it does | Why the picture cannot change |
| --- | --- | --- |
| `stage_cull.py` | Drops blocks whose bounding box, grown by the worst-case stroke reach (half the line width times the miter limit) plus 10 pt, misses one of the active clip regions. | Painting is limited to the intersection of the clip regions. A shape that misses one of them paints nothing. |
| `stage_trim.py` | Cuts straight 2-point stroke segments to the part inside their clip regions grown by line width + 10 pt. Zero-length strokes are kept as they are. | A 2-point segment has no joins, so collinear pieces paint the same pixels inside the clip. The removed parts and the new end caps are at least 10 pt outside the clip. |
| `stage_fold.py` | Merges adjacent `q <clip> W* n <state> ... Q` groups that repeat the identical clip and state. | Clipping to C twice equals clipping once. Same state, same order. |
| `stage_split.py` | Splits long polylines that use round joins and round caps into pieces of about 100 pt that share their cut vertices. | With round joins and caps, a stroke is the path swept by a disk, which equals the union of the pieces swept by the same disk. Coordinates are reused as text, unchanged. |
| `stage_chunk.py` | Groups consecutive blocks with the same graphics state by location and moves larger groups into Form XObjects with a tight BBox; small clip groups become forms too. | Shapes painted in one opaque colour give the same result in any order; other shapes only move past groups they do not overlap. Each form's BBox contains everything it paints (checked again by the verifier). Viewers skip forms whose BBox is off screen. |
| `stage_assemble.py` | Puts the new content stream and forms into the source document. | Only the page content and new `/Ck*` forms change. |

No shape is merged into another path. Merging would change how overlapping edges are
anti-aliased (double-drawn hatching, for example, would get lighter).

## What the optimizer refuses

The stages stop with an error rather than guess when a chart uses more than one page,
`ExtGState` (transparency, blend modes), dash patterns, an explicit miter limit, or colour
operators other than CMYK `k`/`K` between drawing blocks. Content under a non-identity transform
is left inline instead of being moved into forms. A refusal means the chart needs new, verified
support.

## verify_chart.py: the exact check

The verifier shares no code with the optimizer. It parses both files with qpdf's tokenizer
(through pikepdf), expands Form XObjects, and turns every painting operation into a record:

- **kind**: shape, stroke segment, text, image;
- **style**: paint operator, fill and stroke colour, line width, cap, join, miter limit, dash,
  rendering intent, flatness; for text the font (by content), size, spacing, scaling, leading,
  rise, render mode and colour;
- **clip stack**: the exact clip paths in effect;
- **layer stack**: the optional-content groups the operation belongs to;
- **geometry**: absolute coordinates (rounded to 1e-6 pt).

Strokes with round joins and round caps are recorded segment by segment, because their painted
area is exactly the union of the segment strokes. That makes split polylines compare equal while
every segment still has to be there.

The verdict is PASS only if all of these hold:

- **A. Document.** Catalog (layers panel, metadata, viewer settings), document info, page boxes,
  `/VP` georeferencing, annotations, fonts, layer properties and the source images are identical.
  New XObjects may only be Form XObjects.
- **B. Nothing added.** Each optimized record matches a source record, k-th copy to k-th copy.
  The only exception is a trimmed stroke: straight pieces lying on a source segment (within
  0.001 pt) with the same style, clip and layers, not overlapping each other, with one copy of the
  pieces per source copy.
- **C and D. Nothing visible removed.** Each remaining source record must be provably hidden, and
  so must each cut-away part of a trimmed stroke. "Provably hidden" is exact geometry: a capsule
  that contains the record's painted area (control-point box or segment, grown by the worst-case
  stroke reach plus 1 pt) must not come within its radius of any edge of one of its clip paths,
  and one point of it must lie outside that clip region under the clip's fill rule (ray casting).
  If no clip edge enters the capsule, the whole capsule is on one side of the clip boundary.
  Curved clip edges are handled through their control polygons; the checker refuses to decide when
  a capsule starts inside a curve's control box.
- **E. Paint order.** The sequence of runs of identical style, clip and layers is the same, every
  record stays in its run, and in runs that do not paint a single opaque colour, overlapping
  records keep their relative order.
- **F. Forms.** Everything painted inside a Form XObject lies inside its BBox, which would
  otherwise clip it.
- **G. Text.** The text-showing operations match one for one in order, and MuPDF's
  character-level extraction (glyphs, positions, fonts, colours) is identical.

Anything it cannot model (transparency, shadings, patterns, inline images, text used as a clip,
rotated forms, Type 3 fonts) makes it stop with UNSUPPORTED instead of guessing.

When a record is reported missing, the message gives its location, layers, style, and for each
clip the distance to the nearest clip edge and whether the record is inside the clip region. That
is usually enough to tell a real loss from a too-strict margin.

## render_compare.py: what real engines draw

The exact check is about the PDF's content; this check is about what viewers draw. It renders
both files with PDFium (Chrome, Android, many apps), MuPDF and, on macOS, CoreGraphics (Preview,
Safari, iOS) at 216 dpi in tiles over the whole page.

Files that are geometrically identical can still differ by a few anti-aliasing levels: a trimmed
stroke is rasterised from different end points, and a shape moved into a form may be composited
slightly differently. Missing or added content looks different: ink appears or disappears. Two
detectors run on every tile:

- **ink**: both renders are blurred (Gaussian, sigma 1.5 px), which cancels sub-pixel shifts of
  the same ink. A connected area where a colour channel differs by more than 24 of 255 is a
  change. On a 15 MB ArcGIS chart, equivalent files stay at 10 to 13 in all three engines.
- **area**: the mean signed colour difference over 4 pt squares. More than 12 is a change. This
  catches a lighter or darker tint over an area, such as a stipple band an engine failed to draw.

Every change is reported with a side-by-side crop (source | optimized | difference).

Pixel checks cannot see everything. A hairline mark a fraction of a pixel wide, or content
covered by other layers, stays below any sane threshold. That is why the exact check comes first
and the render check is a second, independent line of evidence.

The script also times opening and drawing the whole page at phone width (1600 px) for both files
in each engine. Those are the numbers to report as the speed-up.

## mutation_test.py: checking the checkers

A checker that never fails proves nothing. This script plants one defect at a time in copies of
the optimized file and requires `verify_chart.py` to fail every one:

drop-dot, drop-stroke, shorten-stroke, drop-text, drop-group, move-shape, recolour,
duplicate-shape, remove-clip, shrink-form, reorder-text, change-layer, change-georef.

For defects that change a visible area (drop-text, drop-group, remove-clip) it also requires
`render_compare.py` to fail around that spot. For the others (a single sub-point dot, a 1% colour
change, a 0.5 pt move) the render result is shown for information only, because such changes can
be smaller than the anti-aliasing differences between equivalent files.

Targets are picked at random (fixed seed) among shapes well inside their clip region, so each
defect is a real visible change.

## Limits

- Single-page charts only. The optimizer is tuned to ArcGIS/ArcMap exports and refuses
  structures it does not understand; other producers will mostly be refused rather than broken.
- Georeferencing (`/VP`) is copied unchanged and checked byte for byte, but not tested in GPS
  apps.
- CoreGraphics is only available on macOS. Elsewhere `render_compare.py` uses PDFium and MuPDF.
- The checkers model what a PDF says to paint. They cannot catch an engine bug that only shows up
  with the optimized structure; the render comparison covers the three most common engines.
