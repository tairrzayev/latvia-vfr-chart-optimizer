# Latvia VFR Chart Optimizer

An agent skill that makes the Latvian VFR chart PDF open fast on laptops, phones and tablets,
and then proves, against the source chart, that nothing was lost.

The official digital chart is painfully slow to open: the Latvian ICAO 1:500 000 VFR chart,
exported from ArcGIS, is a single 141 x 62 cm page with 227 MB of drawing instructions, most of them
dots and hatch lines that are masked away and never visible. This skill removes the invisible
work and restructures the rest so viewers can skip what is off screen. Every layer, label,
symbol, colour, the georeferencing and the metadata stay exactly as published.

Because a chart is used to fly, the result is treated as untrusted until three independent
checks pass:

1. **Exact geometric comparison** (`verify_chart.py`). Every drawing operation of both files is
   compared: shape, colour, line style, clip, layer, paint order, text. Anything missing must be
   *proven* hidden by its clip path with exact geometry.
2. **Rendering comparison** (`render_compare.py`). PDFium (Chrome, Android), MuPDF and
   CoreGraphics (Preview, iOS) render both files at 216 dpi over the whole page; any place where
   ink or tint appears or disappears is reported with a side-by-side crop.
3. **Planted defects** (`mutation_test.py`). Thirteen kinds of realistic defects are planted in
   copies of the output, and the checkers must catch every one.

To see it for yourself, the rendering comparison can also write a **visual diff map**, like the
failure images of a golden test: a page that opens offline in any browser and shows the two
renders as a difference view (differing pixels coloured over the faded source), side by side, as a
swipe or blinking, zoomable down to single pixels, for each engine.

## Results on the Latvian VFR chart (15th edition)

| | Source | Optimized |
| --- | --- | --- |
| File size | 15.4 MB | 10.2 MB |
| Drawing instructions on the page | 227 MB | 5 MB + 1,545 location-bounded groups |
| Open and draw at phone width, PDFium (Chrome, Android) | 6.7 s | 1.3 s |
| Same, MuPDF | 6.4 s | 1.6 s |
| Same, CoreGraphics (Preview, iOS) | 3.7 s | 1.1 s |

Measured on an Apple M1. Zoomed-in views gain more, because viewers skip whole off-screen
groups. Verification: 1,774,854 source drawing records compared, 943,829 removed records proven
hidden by their clip masks (99.5% of them from the airspace buffer pattern), 42,427 text
characters identical, no rendering change above the noise floor in any engine, and all 13
planted defects caught. The next issue (WEF 29 Oct 2026) also passes the exact and rendering
checks.

## Install

As a Claude Code plugin:

```bash
claude plugin marketplace add tairrzayev/latvia-vfr-chart-optimizer
claude plugin install latvia-vfr-chart-optimizer@latvia-vfr-chart-optimizer
```

Or copy `skills/optimize-vfr-chart/` into `~/.claude/skills/` (all projects) or a project's
`.claude/skills/`. Other agents that read `SKILL.md` folders can use the same directory.

Requirements: Python 3.10+ (the skill creates its own virtual environment in
`~/.cache/optimize-vfr-chart`, using `uv` when available). On macOS, the CoreGraphics renderer
is compiled with `swiftc` on first use; without it, the render check uses PDFium and MuPDF.

## Use

Ask your agent, for example:

- "Get the latest VFR chart and make it fast on my phone."
- "Optimize ~/Downloads/VFR_Chart.pdf."

It asks whether to download the currently effective chart from the
[Latvian AIS](https://ais.lgs.lv/page/VFR_Suport) or use your file, and whether you want the
visual diff map, then optimizes, runs the three checks and reports the result. A file that fails
any check is renamed `*.REJECTED.pdf`.

The scripts also work without an agent:

```bash
PY="$(skills/optimize-vfr-chart/scripts/setup.sh)"
S=skills/optimize-vfr-chart/scripts
"$PY" $S/fetch_chart.py --list                       # published issues and WEF dates
"$PY" $S/fetch_chart.py                              # download the currently effective one
"$PY" $S/optimize_chart.py VFR_Chart.pdf             # writes VFR_Chart_optimized.pdf
"$PY" $S/verify_chart.py VFR_Chart.pdf VFR_Chart_optimized.pdf
"$PY" $S/render_compare.py VFR_Chart.pdf VFR_Chart_optimized.pdf --diff-map diff-map  # then open diff-map/index.html
"$PY" $S/mutation_test.py VFR_Chart.pdf VFR_Chart_optimized.pdf
```

How each step works, and why each change is safe:
[skills/optimize-vfr-chart/references/how-it-works.md](skills/optimize-vfr-chart/references/how-it-works.md).

## Scope

Built and tested on the Latvian ICAO 1:500 000 VFR chart (the 3 Sep and 29 Oct 2026 issues),
which ArcGIS/ArcMap exports as one page. Other charts are untested. Anything the optimizer or the
checkers do not understand (transparency, dash patterns, shadings, multi-page files, ...) makes
them stop instead of guessing, so a different chart is more likely to be refused than broken. The
AIS download only knows the Latvian AIS page.

## Development

```bash
python -m pip install -r tests/requirements-dev.txt
python -m pytest -q tests
```

The tests build a small synthetic chart (`tests/make_chart.py`), so no real chart is needed. See
[CLAUDE.md](CLAUDE.md) for the rules that keep the checkers trustworthy.

## Disclaimer

Not affiliated with LGS or any aeronautical information service. Charts are published by their
issuing authorities and remain their property; this repository contains no chart data. The
optimized file is a convenience copy of the chart you supplied or downloaded: check that the
issue is current, and use it together with the AIP, NOTAMs and the rest of your preflight
information. Provided without warranty (see [LICENSE](LICENSE)).
