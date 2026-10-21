---
name: optimize-vfr-chart
description: Make a huge vector VFR/aeronautical chart PDF (ArcGIS/ArcMap export, such as the Latvian ICAO 1:500 000 VFR chart) open several times faster on laptops, phones and tablets without losing any detail, then prove nothing was lost by comparing it against the source chart with an exact geometric checker, three PDF rendering engines and planted-defect tests, and optionally a visual diff map (golden-test style) to inspect in a browser. Can download the currently effective chart from the Latvian AIS website or use a PDF the user provides. Use when the user wants to optimize, speed up, slim down or fix lag in a VFR chart, aeronautical chart or very large map PDF, or asks for the latest VFR chart in a fast-opening form.
---

# Optimize a VFR chart PDF

A VFR chart is used to fly. A symbol, label, airspace line or layer that silently disappears is a
safety problem, so this workflow treats the optimizer as untrusted: the result is only handed
over after three independent checks against the source chart all pass.

Paths below are relative to this skill's directory. Each check prints a final `VERDICT:` line;
read it, and keep the full output for the report. The whole run takes about 20 minutes for a
15 MB chart, most of it verification; tell the user that before starting.

## Ground rules

- Never modify, move or overwrite the source PDF. The optimizer checks its SHA-256 before and after.
- Never hand over an optimized file unless `verify_chart.py`, `render_compare.py` and
  `mutation_test.py` all end in `VERDICT: PASS`.
- Never loosen a threshold, edit a checker, or explain away a failure to get a PASS. A failure is
  the result. Report it with the `PROBLEM:` lines and crops, and keep the file out of use.

## 1. Choose the chart and the outputs

Ask what the user has not already said, both questions at once (use your question tool if you
have one):

1. **Which chart**:
   - **Latest effective chart from AIS**: download the currently effective issue from
     https://ais.lgs.lv/page/VFR_Suport (Latvia).
   - **A PDF file**: they give the path to a chart they already have.
2. **Visual diff map** (recommended): a page they can open in a browser to see for themselves
   that the two files look the same, like the failure images of a golden test: differing pixels
   over the faded source, side by side, swipe and blink, zoomable to single pixels. It adds about
   10 seconds and 60 MB of images per rendering engine.

## 2. Set up the environment

```bash
PY="$(scripts/setup.sh)"   # creates or reuses a venv in ~/.cache/optimize-vfr-chart, prints its python
```

Needs Python 3.10+ (uses `uv` when available). On macOS, `render_compare.py` also builds a small
CoreGraphics renderer with `swiftc` on first use; without Xcode tools it skips that engine.

## 3. Get the source PDF

- **AIS download**: run `"$PY" scripts/fetch_chart.py --out-dir <dir>` (default: the current
  directory). It lists every published issue with its WEF (effective from) date, downloads the
  one the page marks as currently effective, and stops if that label disagrees with the dates.
  Tell the user the WEF date, and mention any newer issue it lists as upcoming (they can get it
  with `--issue next` or `--issue DDMMMYYYY`).
- **User file**: use it in place. If it is not a single-page PDF the checkers will refuse it.

## 4. Optimize

```bash
"$PY" scripts/optimize_chart.py SOURCE.pdf [OUTPUT.pdf]   # about 2-3 minutes for a 15 MB chart
```

The default output is `<source stem>_optimized.pdf` next to the source; the script refuses to
overwrite an existing file, so pick another name or ask the user if one exists. If a stage
fails (for example the chart uses transparency or dashed lines), stop and report it: this chart
needs support that does not exist yet.

## 5. Verify against the source

Keep the reports in a folder next to the output, `CHECKS=<output stem>_checks`. Run the first
two in parallel, and the third after the first has finished (it reuses its cache).

```bash
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/optimize-vfr-chart/verify-cache"
"$PY" scripts/verify_chart.py SOURCE.pdf OUTPUT.pdf --cache "$CACHE" --json "$CHECKS/verify.json"  # ~4 min
"$PY" scripts/render_compare.py SOURCE.pdf OUTPUT.pdf --out "$CHECKS/crops" --json "$CHECKS/render.json" \
    --diff-map "$CHECKS/diff-map"  # ~4 min; leave out --diff-map if the user did not want the map
"$PY" scripts/mutation_test.py SOURCE.pdf OUTPUT.pdf --cache "$CACHE"  # ~11 min
```

- `verify_chart.py` is the main proof. It reads every drawing operation of both files and
  requires each source shape, text run, image, clip, layer membership and paint order to be
  present in the output, except shapes it can prove geometrically are hidden by their clip path.
- `render_compare.py` renders both files with PDFium, MuPDF and CoreGraphics and reports any
  place where ink appears or disappears, with side-by-side crops (source | optimized | diff).
  With `--diff-map` it also writes `$CHECKS/diff-map/index.html` (the viewer) and an
  `overview-<engine>.png` per engine from the same renders. The map is for people to look at; it
  does not change the verdict.
- `mutation_test.py` plants 13 realistic defects (a missing dot, a shortened line, a moved
  shape, a removed clip, a changed layer or georeference, ...) in copies of the output and
  requires the checkers to catch every one. It shows that the PASS above is meaningful.

For what each stage and check does, and why each change is safe, read
[references/how-it-works.md](references/how-it-works.md).

## 6. Decide

- **All three PASS**: deliver the optimized file.
- **Anything else**: rename the output to `<name>.REJECTED.pdf` so it cannot be mistaken for a
  good file, and report each `PROBLEM:` line. For render failures, open the crop images listed in
  the output and look at them before describing the difference; the diff map, if made, marks the
  same places with numbered red boxes. Do not retry with different
  settings unless the user asks.

## 7. Report

Keep it short and concrete:

- Which chart (file name, WEF date if downloaded, SHA-256 prefix), where the output is, and the
  `_checks` folder with the reports.
- File size before and after, and the render timings from `render_compare.py` (time to open
  and draw the whole page at phone width, per engine).
- The three verdicts, with the key numbers: shapes compared, shapes removed as provably hidden
  (by layer), trimmed strokes, text characters compared, pixel differences per engine, defects
  caught.
- If a diff map was made: the path to its `index.html`, which opens offline in any browser
  (offer to open it, for example with `open` on macOS). Look at one overview image first and
  say in a sentence what it shows: where pixels differ, by how much, and whether any red box
  marks a change.
- Any upcoming chart issue the AIS page lists, so the user knows when to run this again.
