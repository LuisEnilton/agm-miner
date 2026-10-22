"""Stage 4 — export the raw ("bronze") dataset: every table as Parquet plus a manifest.

The bronze export is the unit of replication: the analysis stage reads only these files, never
the database, so a published export can be re-analysed without re-mining. Parquet schemas are
derived from the ORM models (not inferred from data), so empty or all-NULL columns keep their
types, and tables are streamed in batches to bound memory use.
"""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow.parquet as pq
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Integer,
    String,
    Table,
    Text,
    cast,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from agm_miner.config import StudyConfig
from agm_miner.database.models import Base, RunStage
from agm_miner.runs import code_version, track_run

logger = logging.getLogger(__name__)

MANIFEST = "manifest.json"
_BATCH_ROWS = 20_000


def polars_dtype(column_type: Any) -> pl.DataType:
    if isinstance(column_type, JSONB):
        return pl.Utf8()
    if isinstance(column_type, ARRAY):
        return pl.List(polars_dtype(column_type.item_type))
    if isinstance(column_type, Boolean):
        return pl.Boolean()
    if isinstance(column_type, Integer):
        return pl.Int64()
    if isinstance(column_type, DateTime):
        return pl.Datetime("us", "UTC")
    if isinstance(column_type, Date):
        return pl.Date()
    if isinstance(column_type, Enum | String | Text):
        return pl.Utf8()
    raise TypeError(f"No Parquet mapping for column type {column_type!r}")


def table_schema(table: Table) -> dict[str, pl.DataType]:
    return {column.name: polars_dtype(column.type) for column in table.columns}


def _select(table: Table):
    columns = [
        cast(column, Text).label(column.name) if isinstance(column.type, JSONB) else column
        for column in table.columns
    ]
    return select(*columns).order_by(*table.primary_key.columns)


def export_table(
    connection: Connection,
    table: Table,
    destination: Path,
    on_rows: Callable[[int], None] | None = None,
) -> int:
    """Stream ``table`` into ``destination``; ``on_rows(n)`` is called after each batch."""
    schema = table_schema(table)
    rows = 0
    writer: pq.ParquetWriter | None = None
    try:
        result = connection.execution_options(stream_results=True).execute(_select(table))
        while batch := result.fetchmany(_BATCH_ROWS):
            frame = pl.DataFrame(
                [tuple(row) for row in batch], schema=schema, orient="row", strict=False
            )
            arrow = frame.to_arrow()
            if writer is None:
                writer = pq.ParquetWriter(destination, arrow.schema)
            writer.write_table(arrow)
            rows += frame.height
            if on_rows is not None:
                on_rows(frame.height)
        if writer is None:
            pl.DataFrame(schema=schema).write_parquet(destination)
    finally:
        if writer is not None:
            writer.close()
    return rows


def export_bronze(
    study: StudyConfig,
    session_factory: sessionmaker[Session],
    output_root: Path,
) -> Path:
    """Write ``<output_root>/<timestamp>/`` with one Parquet file per table and a manifest.

    All tables are read inside one REPEATABLE READ transaction, so the export is a consistent
    snapshot even while ``agm mine`` keeps writing. Files are written to ``<timestamp>.partial``
    and renamed at the end: an interrupted export never looks like a complete one.
    """
    engine: Engine = session_factory.kw["bind"]
    started = datetime.now(UTC)
    destination = output_root / started.strftime("%Y%m%dT%H%M%SZ")
    staging = destination.with_name(destination.name + ".partial")
    staging.mkdir(parents=True, exist_ok=False)
    try:
        _export_into(study, session_factory, engine, started, staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    staging.rename(destination)
    return destination


def _export_into(
    study: StudyConfig,
    session_factory: sessionmaker[Session],
    engine: Engine,
    started: datetime,
    staging: Path,
    destination: Path,
) -> None:
    with (
        track_run(session_factory, RunStage.EXPORT, study, {"output": str(destination)}) as run,
        engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection,
        connection.begin(),
    ):
        progress = run.progress
        all_tables = Base.metadata.sorted_tables
        progress.set_phase("counting rows")
        progress.set_total("tables", len(all_tables))
        progress.set_total(
            "rows",
            sum(
                connection.execute(select(func.count()).select_from(table)).scalar_one()
                for table in all_tables
            ),
        )
        tables: dict[str, int] = {}
        for table in all_tables:
            progress.set_phase(f"exporting {table.name}")
            count = export_table(
                connection,
                table,
                staging / f"{table.name}.parquet",
                on_rows=lambda rows: progress.advance("rows", rows),
            )
            tables[table.name] = count
            progress.advance("tables")
            logger.info("Exported %s rows from %s", count, table.name)
        schema_revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()
        manifest = {
            "exported_at": started.isoformat(),
            "export_run_id": run.id,
            "code_version": code_version(),
            "schema_revision": schema_revision,
            "study_config_hash": study.fingerprint(),
            "study_config": study.snapshot(),
            "tables": tables,
        }
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        run.stats.update({f"rows:{name}": count for name, count in tables.items()})


def read_manifest(bronze_dir: Path) -> dict[str, Any]:
    return json.loads((bronze_dir / MANIFEST).read_text(encoding="utf-8"))


def latest_export(output_root: Path) -> Path:
    exports = sorted(
        path
        for path in output_root.iterdir()
        if not path.name.endswith(".partial") and (path / MANIFEST).exists()
    )
    if not exports:
        raise FileNotFoundError(f"No bronze export found in {output_root}")
    return exports[-1]
