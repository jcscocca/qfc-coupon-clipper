# Repo Cleanup — Light Tidy — Design

**Date:** 2026-06-24
**Status:** Approved in brainstorming; mechanical execution (no separate plan cycle, per user)

## Problem

The repo root holds 12 tracked files with source, tests, config, deps, and scripts all
intermingled flat. The user wants a cleaner root without changing how the tool is run.

## Decisions (from brainstorming)

- **Scope:** light tidy — group peripheral files into `tests/` and `scripts/`. Source
  modules, config, and requirements stay at root.
- **Run command unchanged:** `qfc_coupon_clipper.py` stays at root, so
  `python qfc_coupon_clipper.py` and its `Path(__file__).parent / "config.toml"`
  discovery are untouched.
- **Launcher stays at root:** the planned `launch.sh` / `launch.command` remain at root
  for Finder double-click discoverability. Only `run.sh` and `setup.sh` move to `scripts/`.
- **Branch:** dedicated `repo-cleanup` branch off `main`, independently mergeable.

## Target layout

```
qfc-coupon-clipper/
├── .gitignore  LICENSE  README.md
├── config.example.toml  (config.toml)
├── requirements.txt  requirements-dev.txt
├── qfc_coupon_clipper.py  relevance.py
├── conftest.py                 # NEW
├── scripts/{run.sh, setup.sh}
├── tests/{test_qfc_clipper.py, test_relevance.py}
├── docs/  scheduling/
```

Root: 12 tracked files → 8.

## Changes

### Move tests → `tests/` (git mv)
- Add root `conftest.py` that inserts the repo root onto `sys.path`.
- Remove the per-file `sys.path.insert(...)` bootstrap and the now-unused
  `import sys` / `from pathlib import Path` / `# noqa: E402` markers in both tests.
- Fix the stale `pytest jobs/qfc_clipper` comment in `test_qfc_clipper.py`.
- Plain `pytest` from root still auto-discovers `tests/` — README test command unchanged.

### Move `run.sh`, `setup.sh` → `scripts/` (git mv)
- In each: `cd "$(dirname "$0")"` → `cd "$(dirname "$0")/.."` so they operate from repo
  root (where `.venv`, `requirements.txt`, the entry script, and `logs/` live).
- Update self-referential usage comments and the `run ./setup.sh first` message.
- README: `./setup.sh` → `./scripts/setup.sh`; the two `run.sh` mentions → `scripts/run.sh`.
- Plist: `ProgramArguments` path → `…/qfc-coupon-clipper/scripts/run.sh`.

### Unchanged
`qfc_coupon_clipper.py`, `relevance.py`, `config.example.toml`, `requirements*.txt`,
`.gitignore` (its patterns are unanchored / dir-based and still match).

## Verification

- `.venv/bin/python -m pytest -q` → expect **27 passed** (21 in test_relevance + 6 in
  test_qfc_clipper).
- `bash -n scripts/run.sh scripts/setup.sh` → syntax OK.
- Grep confirms no stale root-level `run.sh` / `setup.sh` / `test_*` paths remain.

## Interaction with the launcher work

The one-click-launcher spec (on its own branch) references `./setup.sh`; once `setup.sh`
lives in `scripts/`, the launcher's `launch.sh` must call `./scripts/setup.sh`. The
launcher stays at root. This update is tracked in the deferred launcher task and applied
when that work resumes.

## Out of scope (YAGNI)

Package/`src` layout, `pyproject.toml`/`pytest.ini`, moving config or requirements,
Windows, touching the clipping logic.
