# Working on this repo

This repo ships one agent skill, [`optimize-vfr-chart`](skills/optimize-vfr-chart/SKILL.md), as a
Claude Code plugin (`.claude-plugin/`). `.claude/skills/optimize-vfr-chart` is a symlink so the
skill is also available when working inside this repo.

## Layout

- `skills/optimize-vfr-chart/SKILL.md`: the workflow the agent follows. Keep it short; put detail
  in `references/`.
- `skills/optimize-vfr-chart/scripts/stages/`: the optimizer, one stage per file.
- `skills/optimize-vfr-chart/scripts/verify_chart.py`, `render_compare.py`, `mutation_test.py`:
  the checkers.
- `tests/`: pytest suite on a synthetic chart (`tests/make_chart.py`), no real chart needed.

## Rules

- The checkers must stay independent of the optimizer: never import from `scripts/stages/` in a
  checker, and never share parsing or geometry code between them.
- Never relax a checker (tolerance, threshold, margin) to make an optimizer change pass. If a
  check fails, the optimizer is wrong until proven otherwise.
- An optimizer change needs: `pytest -q tests` passing, and all three checks passing on a real
  chart (see the skill's step 5). Add a stage only with a short argument in
  `references/how-it-works.md` for why it cannot change the picture.
- No chart PDFs in the repo (`.gitignore` blocks them). Charts belong to their publishers.
- No em-dashes in prose (Markdown, docstrings, comments, script output).
- Run `claude plugin validate .` after editing anything in `.claude-plugin/`. Its one expected
  warning is that this file is not shipped with the plugin; it is meant for contributors.
