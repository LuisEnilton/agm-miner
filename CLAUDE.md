# CLAUDE.md

The project guide for coding agents lives in AGENTS.md (shared by every tool, so it cannot drift):

@AGENTS.md

Claude-specific notes:

- Always add or update pytest coverage together with a change and run `poetry run pytest` plus
  `poetry run ruff check .` before reporting the work as done.
- Never run the pipeline against the development database for experiments; use a separate
  `DATABASE_URL` (e.g. `agm_validation`) and a scratch `DATA_DIR`.
