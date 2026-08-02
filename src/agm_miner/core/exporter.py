from __future__ import annotations

import logging
from pathlib import Path

import polars as pl
from sqlalchemy.engine import Engine

from agm_miner.config import Settings


logger = logging.getLogger(__name__)


def _cast_problematic_columns(frame: pl.DataFrame) -> pl.DataFrame:
    columns_to_cast = [
        "id",
        "artifact_id",
        "status",
        "artifact_type",
        "author_type",
    ]
    existing_columns = [column for column in columns_to_cast if column in frame.columns]

    if not existing_columns:
        return frame

    return frame.with_columns(
        [
            pl.col(column).map_elements(
                lambda x: str(x) if x is not None else None,
                return_dtype=pl.Utf8,
            )
            for column in existing_columns
        ]
    )


class DatasetExporter:
    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine

    def export(self) -> list[Path]:
        output_dir = Path(self.settings.gold_data_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        exported_files: list[Path] = []

        queries = {
            "repositories.parquet": """
                SELECT
                    r.id,
                    r.full_name,
                    r.stars,
                    r.forks,
                    r.contributors_count,
                    r.total_commits,
                    r.has_md_instructions,
                    r.has_skill_files,
                    r.created_at
                FROM repositories r
            """,
            "commits.parquet": """
                SELECT
                    ac.commit_hash,
                    ac.artifact_id,
                    ac.author_login,
                    ac.authored_at,
                    ac.commit_message,
                    ac.additions,
                    ac.deletions,
                    ac.time_since_last_update_hours,
                    a.file_path,
                    a.artifact_type,
                    au.author_type,
                    au.repo_id
                FROM artifact_commits ac
                JOIN agent_artifacts a ON a.id = ac.artifact_id
                JOIN authors au ON au.login = ac.author_login
            """,
        }

        for filename, query in queries.items():
            frame = pl.read_database(query, connection=self.engine)
            frame = _cast_problematic_columns(frame)
            destination = output_dir / filename
            frame.write_parquet(destination)
            exported_files.append(destination)

        logger.info("Exported %s parquet files to %s", len(exported_files), output_dir)
        return exported_files