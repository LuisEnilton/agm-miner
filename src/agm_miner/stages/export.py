"""Stage 4 — export the raw ("bronze") dataset: every table as Parquet plus a manifest.

The bronze export is the unit of replication: the analysis stage reads only these files, never
the database, so a published export can be re-analysed without re-mining. Parquet schemas are
derived from the ORM models (not inferred from data), so empty or all-NULL columns keep their
types, and tables are streamed in batches to bound memory use.
"""

from __future__ import annotations

import json
import logging
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
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.engine import Engine
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


def export_table(engine: Engine, table: Table, destination: Path) -> int:
    schema = table_schema(table)
    rows = 0
    writer: pq.ParquetWriter | None = None
    try:
        with engine.connect() as connection:
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
    """Write ``<output_root>/<timestamp>/`` with one Parquet file per table and a manifest."""
    engine: Engine = session_factory.kw["bind"]
    started = datetime.now(UTC)
    destination = output_root / started.strftime("%Y%m%dT%H%M%SZ")
    destination.mkdir(parents=True, exist_ok=False)
    with track_run(session_factory, RunStage.EXPORT, study, {"output": str(destination)}) as run:
        tables: dict[str, int] = {}
        for table in Base.metadata.sorted_tables:
            count = export_table(engine, table, destination / f"{table.name}.parquet")
            tables[table.name] = count
            logger.info("Exported %s rows from %s", count, table.name)
        with engine.connect() as connection:
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
        (destination / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        run.stats.update({f"rows:{name}": count for name, count in tables.items()})
    return destination


def read_manifest(bronze_dir: Path) -> dict[str, Any]:
    return json.loads((bronze_dir / MANIFEST).read_text(encoding="utf-8"))


def latest_export(output_root: Path) -> Path:
    exports = sorted(path for path in output_root.iterdir() if (path / MANIFEST).exists())
    if not exports:
        raise FileNotFoundError(f"No bronze export found in {output_root}")
    return exports[-1]
