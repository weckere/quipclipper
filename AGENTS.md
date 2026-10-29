# AGENTS.md: instructions for every coding agent working on quipclipper

This file is for any agent (OpenCode, Claude Code or another) that opens this repo. If a
`CLAUDE.md` sits next to it, read that too: it holds the owner's local working notes and
applies to you as well. Start with README.md, docs/DESIGN_NOTES.md and docs/API.md.

## What this is

quipclipper finds a moment in a movie, show, podcast or audiobook by its dialogue and cuts it
losslessly: an engine + CLI (`src/quipclipper/`), a FastAPI web app (`web/backend/`,
`web/frontend/`), Docker images and a NixOS module (`nix/`).

## How work lands

- **Work on `dev`.** Linear history, no pull requests: branch a worktree from `origin/dev`,
  rebase onto it, push to `dev`. CI runs on pushes to `dev` and `main`.
- **`main` is release-only.** Never push or commit on it. Only `scripts/release.sh X.Y.Z`
  moves it, and only when the owner says to cut a release (rehearse with `--dry-run`).
- **Every commit that changes behaviour adds a line to CHANGELOG.md's `## [Unreleased]`**
  in the same commit. Only the release script changes the version (`pyproject.toml` and
  its copy in `web/backend/pyproject.toml`; `tests/test_version.py` fails otherwise).
- **Every web UI action needs a CLI command** in `src/quipclipper/commands.py`; the parity
  tests in `web/backend` fail until it has one.
- **Tests must be deterministic** (no timing, no unseeded randomness): the Nix build runs the
  whole suite on a busy machine. Web tests that need `web/frontend` skip when it is absent.

## Done means

```bash
pytest -q                        # engine + CLI, at the repo root
(cd web/backend && pytest -q)    # web app, CLI parity, frontend guards
```

Both green on the rebased tree before pushing, and a UI change checked in a browser against
real data.

## Keep private things out

The repo is public. Never commit credentials, media, personal data, or references to the
owner's private infrastructure (host names, task boards, notes). `CLAUDE.md` and
`.claude/worktrees/` are deliberately untracked.
