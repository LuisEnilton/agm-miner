# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`agm-miner` is a FastAPI service that mines GitHub for agent-instruction artifacts (`CLAUDE.md`, `AGENTS.md`, `.claude/skills/**` files), stores structured metadata + commit history in Postgres, and exports the result as an analytical (parquet) dataset. It's a research data pipeline (MSR-style study of AI agent config files in the wild), not a user-facing product — there is no frontend.

## Environment setup

- Requires Python 3.11 (`.python-version` pins it; Poetry will otherwise try 3.13 and fail — force it with `poetry env use /usr/bin/python3.11`).
- Dependency/venv manager is Poetry; the importable package lives under `src/agm_miner` (`src`-layout).
- Config is loaded from a `.env` file (see `.env.sample`) via `agm_miner.config.Settings` (pydantic-settings). `GITHUB_TOKEN` and `DATABASE_URL` are required with no defaults; the app will fail to start without them.

```bash
cp .env.sample .env               # fill in GITHUB_TOKEN, DATABASE_URL, etc.
poetry env use /usr/bin/python3.11
poetry install --no-root
docker compose up -d db           # local Postgres 15 on :5432
poetry run alembic upgrade head
```

## Common commands

```bash
poetry run agm-miner                                                          # run via entry point (main:run)
poetry run uvicorn agm_miner.main:app --reload --host 0.0.0.0 --port 8000     # run directly with reload

poetry run ruff check .                                                       # lint
poetry run ruff format .                                                      # format
poetry run pytest                                                             # test runner is configured (pytest, pytest-asyncio) but no tests exist yet

poetry run alembic revision --autogenerate -m "message"                       # new migration (models.py is the single source of truth)
poetry run alembic upgrade head
poetry run alembic downgrade -1
```

Health check: `GET /health`. There is no separate CLI for the pipeline stages — they are only reachable as HTTP endpoints (see below), each of which schedules a `BackgroundTasks` job and returns immediately.

## Pipeline architecture

The system is a three-stage pipeline, each stage exposed as a `POST`/`GET` endpoint in [routes.py](src/agm_miner/api/routes.py) that hands off to a service function via FastAPI `BackgroundTasks`. Endpoints return `{"status": "scheduled"}` immediately; work happens async in-process (no queue/worker — this only works because it's a single-instance background job, not a distributed task queue).

1. **Discover** — `POST /api/v1/jobs/discover` → [services/discovery.py](src/agm_miner/services/discovery.py)
   Runs a GitHub *code search* query (e.g. `filename:CLAUDE.md`) and upserts every matching repository into `repositories` with `status=PENDING`. The GitHub Search API caps any single query at 1000 results, so when a query (without an explicit `size:` qualifier) would exceed that, `_discover_with_size_chunks` recursively bisects the `size:min..max` range until each chunk's `totalCount` fits under the cap, then paginates each chunk with `_discover_single_query`. Date-window arguments exist on the endpoint/service signature but are currently not applied to the query (`_compose_query` ignores them) — code search has no reliable date filter.

2. **Mine** — `POST /api/v1/jobs/mine-pending` → [services/mining.py](src/agm_miner/services/mining.py)
   Iterates all `repositories` with `status=PENDING`. For each: fetches repo metadata and skips (marks `COMPLETED` without extraction) any repo under `MIN_STARS_THRESHOLD` (50 stars). For qualifying repos, walks the full git tree (`get_git_tree(..., recursive=True)`) to find target files (`CLAUDE.md`, `AGENTS.md`, anything under `.claude/skills/`), fetches file content (skipping raw content storage above 2MB), and walks per-file commit history (capped at `MAX_COMMITS_PER_FILE` = 50) to populate `authors` and `artifact_commits`. Persistence uses Postgres `INSERT ... ON CONFLICT` upserts (`_upsert_artifact`, `_persist_authors`, and a bulk upsert for `ArtifactCommit`) so mining is safely re-runnable. Each repository is committed and exception-isolated individually — one repo's failure (caught, rolled back, logged) doesn't abort the batch.

3. **Export** — `GET /api/v1/dataset/export` → [core/exporter.py](src/agm_miner/core/exporter.py)
   `DatasetExporter` runs two SQL queries directly against Postgres via `polars.read_database` and writes `repositories.parquet` and `commits.parquet` (joining `artifact_commits` ⋈ `agent_artifacts` ⋈ `authors`) into `settings.gold_data_dir` (default `data/gold/`, gitignored). Columns holding Python enums/UUIDs are stringified before writing because Arrow/parquet can't serialize them directly (`_cast_problematic_columns`).

## GitHub API access layer

All GitHub calls go through [core/github_client.py](src/agm_miner/core/github_client.py)'s `GitHubClient`, which wraps PyGithub with a `@rate_limited(bucket_name)` decorator applied to every public method. There are two independent rate-limit buckets with separate thresholds in `Settings`:
- `core` (`CORE_RATE_LIMIT_MIN_REMAINING`, default 50) — repo metadata, contents, commits.
- `code_search` (`SEARCH_RATE_LIMIT_MIN_REMAINING`, default 2) — `search_code`, GitHub's much stingier Search API.

Before each call the decorator checks remaining quota and sleeps until reset if under threshold; it also catches `RateLimitExceededException` mid-call and retries once after sleeping, and converts `BadCredentialsException` into `GitHubAuthError`. `discovery.py` additionally has its own defensive retry/backoff around 403/abuse responses from `search_code` (separate from the decorator, since GitHub's secondary/abuse rate limits aren't reflected in the `/rate_limit` endpoint). When touching GitHub-calling code, preserve this pacing — it exists specifically to avoid tripping GitHub's abuse detection during large mining runs.

## Data model

Defined in [database/models.py](src/agm_miner/database/models.py), migrated via Alembic ([alembic/versions/](alembic/versions/)). Four tables:

- `repositories` — one row per GitHub repo; `status` enum drives the discover→mine pipeline (`PENDING` → `COMPLETED`/`FAILED`).
- `agent_artifacts` — one row per matched file per repo (`artifact_type`: `ROOT_MD` for `CLAUDE.md`/`AGENTS.md`, `SKILL_FILE` for `.claude/skills/**`); UUID PK.
- `authors` — composite PK `(login, repo_id)`: per-repo commit totals and a classification (`CORE_DEV`/`DOC_ONLY`/`UNKNOWN`).
- `artifact_commits` — one row per commit touching an artifact file, PK'd by `commit_hash`; has a composite FK `(author_login, repo_id) → authors(login, repo_id)` in addition to its FK to `agent_artifacts`, so an `Author` row must exist before its commits can be inserted (mining code respects this ordering).

Alembic's [env.py](alembic/env.py) pulls the DB URL from the app's `Settings` (i.e. from `.env`), not from `alembic.ini`'s `sqlalchemy.url` placeholder — migrations require a valid `.env` to run. `target_metadata = Base.metadata`, so `alembic revision --autogenerate` reflects whatever is in `database/models.py`.

## Layout

```
src/agm_miner/
  main.py              # FastAPI app factory + uvicorn entrypoint
  config.py            # Settings (pydantic-settings, env-backed), get_settings() is lru_cache'd
  api/routes.py         # thin HTTP layer — validates input, schedules BackgroundTasks, no business logic
  services/            # pipeline stages: discovery.py, mining.py (orchestration + GitHub↔DB glue)
  core/                # github_client.py (rate-limited PyGithub wrapper), exporter.py (Postgres→parquet), text_utils.py (path/content predicates)
  database/            # models.py (SQLAlchemy ORM + enums), session.py (engine/session factories)
```

`core/text_utils.py` and the file-classification helpers duplicated inline in `services/mining.py` (`is_markdown_file`, `count_words`, `should_skip_raw_content`) implement the same predicates independently — check both when changing artifact-matching rules (e.g. what counts as a skill file, size cutoffs).
