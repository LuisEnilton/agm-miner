"""Build analysis datasets (silver + gold) from a bronze export.

Silver (one row per parsed artifact version / section):

* ``version_metrics``  — structural and readability metrics of every Markdown version;
* ``version_sections`` — sections with heading paths of every Markdown version;
* ``artifact_sample``  — latest size of every artifact and whether it is in the study sample.

Gold (one table per research question) holds the *study sample* only: artifacts whose latest
version has at least ``min_artifact_bytes``, in repositories with ``min_human_contributors``
human authors (``repo_features`` keeps every mined repository, with the exclusion reason):

* ``rq1_sections``      — sections of the artifacts at HEAD (input for content coding);
* ``rq2_structure``     — metrics of the artifacts at HEAD;
* ``rq3_changes``       — every artifact change with timing, bursts, section/frontmatter diff,
  co-changed file categories, agent involvement and author role;
* ``rq3_author_roles``  — work profile of every contributor;
* ``repo_features``     — repository metrics + aggregated artifact attributes (correlations).
"""

from __future__ import annotations

import json
import logging
import shutil
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow.parquet as pq

from agm_miner.analysis.authors import author_role, category_shares, detect_agent, is_bot
from agm_miner.analysis.history_cutoff import cut_history
from agm_miner.analysis.markdown import ParsedDocument, count_words
from agm_miner.analysis.metrics import clear_syllable_cache, compute_metrics
from agm_miner.analysis.structure_diff import DocumentOutline, diff_documents, outline
from agm_miner.artifacts import ArtifactKind, ArtifactTool
from agm_miner.config import AnalysisConfig
from agm_miner.file_categories import FileCategory
from agm_miner.progress import Progress
from agm_miner.runs import code_version
from agm_miner.stages.export import read_manifest

logger = logging.getLogger(__name__)

BRONZE_TABLES = (
    "repositories",
    "discovery_hits",
    "artifacts",
    "artifact_versions",
    "artifact_changes",
    "commits",
    "commit_files",
    "commit_coauthors",
    "identities",
    "contributor_activity",
    "repository_activity",
)
_MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdx")
ANALYSIS_MANIFEST = "analysis_manifest.json"
_MANUAL_QUERY_PREFIX = "manual:"
# Version contents are read from the bronze export a slice at a time, never whole: ``content``
# is most of the export (gigabytes once decompressed). Each slice is collected by polars and
# handed out in batches (``ParquetFile.iter_batches`` was not used: pyarrow's memory pool kept
# every batch it had read, growing to the size of the whole column).
CONTENT_SLICE_ROWS = 20_000
CONTENT_BATCH_ROWS = 2_000
# ``version_sections`` is read back (for ``rq1_sections``) this many rows at a time.
_SECTIONS_SLICE_ROWS = 200_000
# Tables never loaded whole: aggregated from a scan of the Parquet file instead.
# ``commit_files`` has a row per file of every artifact commit (millions; >1 GB in memory).
SCANNED_TABLES = ("commit_files",)
# Python rows are turned into a frame (or written out) every this many rows.
_CHUNK_ROWS = 20_000
SILVER_VERSION_SECTIONS = "silver_version_sections"


def latest_analysis(gold_root: Path) -> Path:
    """Most recent complete analysis output under ``gold_root`` (``DATA_DIR/gold``)."""
    outputs = sorted(
        path
        for path in (gold_root.iterdir() if gold_root.exists() else [])
        if not path.name.endswith(".partial") and (path / ANALYSIS_MANIFEST).exists()
    )
    if not outputs:
        raise FileNotFoundError(f"No analysis output found in {gold_root} (run `agm analyze`)")
    return outputs[-1]


def load_bronze(bronze_dir: Path) -> dict[str, pl.DataFrame]:
    """The bronze tables, with ``artifact_versions`` *without* its ``content`` column (read in
    batches by ``read_version_contents``) and without ``SCANNED_TABLES`` (``scan_bronze``)."""
    tables = {}
    for name in BRONZE_TABLES:
        if name in SCANNED_TABLES:
            continue
        path = bronze_dir / f"{name}.parquet"
        columns = None
        if name == "artifact_versions":
            columns = [column for column in pq.read_schema(path).names if column != "content"]
        tables[name] = pl.read_parquet(path, columns=columns)
    return tables


def scan_bronze(bronze_dir: Path, name: str) -> pl.LazyFrame:
    return pl.scan_parquet(bronze_dir / f"{name}.parquet")


def _scan_slices(
    scan: pl.LazyFrame, slice_rows: int, predicate: pl.Expr | None = None
) -> Iterator[pl.DataFrame]:
    """The rows of ``scan`` collected ``slice_rows`` at a time, in order (at least one frame,
    so an empty scan still yields its schema), each filtered by ``predicate`` in the query."""
    total = scan.select(pl.len()).collect().item()
    for offset in range(0, max(total, 1), slice_rows):
        query = scan.slice(offset, slice_rows)
        yield (query if predicate is None else query.filter(predicate)).collect()


def read_version_contents(
    bronze_dir: Path,
    batch_rows: int | None = None,
    slice_rows: int | None = None,
) -> Iterator[pl.DataFrame]:
    """``(id, content)`` of every artifact version, a batch at a time, in file order. Only one
    slice of ``slice_rows`` versions is in memory at a time."""
    scan = scan_bronze(bronze_dir, "artifact_versions").select("id", "content")
    for chunk in _scan_slices(scan, slice_rows or CONTENT_SLICE_ROWS):
        yield from chunk.iter_slices(batch_rows or CONTENT_BATCH_ROWS)


def contents_of(versions: pl.DataFrame, batch_rows: int = CONTENT_BATCH_ROWS) -> list[pl.DataFrame]:
    """``(id, content)`` batches of a versions frame that has its contents in memory (tests)."""
    return list(versions.select("id", "content").iter_slices(batch_rows))


class _FrameChunks:
    """Collects rows as Python dicts and turns them into frames every ``_CHUNK_ROWS`` rows, so
    the rows of a large table never sit in memory as Python objects. Each chunk goes to
    ``sink`` when one is given (and nothing is kept), otherwise ``frame()`` concatenates them."""

    def __init__(
        self,
        schema: dict[str, Any],
        sink: Callable[[pl.DataFrame], None] | None = None,
    ) -> None:
        self.schema = schema
        self._sink = sink
        self._rows: list[dict[str, Any]] = []
        self._frames: list[pl.DataFrame] = []
        self.height = 0

    def add(self, row: dict[str, Any]) -> None:
        self._rows.append(row)
        if len(self._rows) >= _CHUNK_ROWS:
            self.flush()

    def extend(self, rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            self.add(row)

    def flush(self) -> None:
        if not self._rows:
            return
        chunk = pl.DataFrame(self._rows, schema=self.schema)
        self._rows = []
        self.height += chunk.height
        if self._sink is not None:
            self._sink(chunk)
        else:
            self._frames.append(chunk)

    def frame(self) -> pl.DataFrame:
        self.flush()
        if not self._frames:
            return pl.DataFrame(schema=self.schema)
        return pl.concat(self._frames, rechunk=True)


class _ParquetSink:
    """Appends frames of one schema to a Parquet file (created even when nothing is written)."""

    def __init__(self, path: Path, schema: dict[str, Any]) -> None:
        self._writer = pq.ParquetWriter(path, pl.DataFrame(schema=schema).to_arrow().schema)

    def __call__(self, frame: pl.DataFrame) -> None:
        self._writer.write_table(frame.to_arrow())

    def close(self) -> None:
        self._writer.close()


def is_markdown_artifact(path: str, kind: str) -> bool:
    return kind != ArtifactKind.SKILL_RESOURCE.value or path.lower().endswith(_MARKDOWN_SUFFIXES)


# --------------------------------------------------------------------------- silver


def build_version_tables(
    tables: dict[str, pl.DataFrame],
    contents: Iterable[pl.DataFrame],
    config: AnalysisConfig,
    sections_path: Path,
    progress: Progress | None = None,
) -> tuple[pl.DataFrame, int, dict[tuple[int, str], DocumentOutline]]:
    """Parse every Markdown version, reading the contents a batch at a time (``contents``:
    ``(id, content)`` frames, from ``read_version_contents``).

    Returns the metrics, the number of section rows, written to ``sections_path`` as they are
    produced (``version_sections`` holds the text of every version, so it never sits in
    memory), and the outline of each parsed blob keyed by (repo_id, blob_sha), for the RQ3
    diffs. A blob shared by several versions is parsed again for each (no parse cache: it would
    hold every document in memory)."""
    versions = (
        tables["artifact_versions"]
        .filter(~pl.col("is_symlink").fill_null(False))
        .join(
            tables["artifacts"].select(pl.col("id").alias("artifact_id"), "path", "kind"),
            on="artifact_id",
        )
    )
    wanted: dict[int, tuple[int, int, str]] = {
        row[0]: (row[1], row[2], row[3])
        for row in versions.select(
            "id", "artifact_id", "repo_id", "blob_sha", "path", "kind"
        ).iter_rows()
        if is_markdown_artifact(row[4], row[5])
    }
    outlines: dict[tuple[int, str], DocumentOutline] = {}
    outline_by_blob: dict[str, DocumentOutline] = {}
    metrics = _FrameChunks(METRICS_SCHEMA)
    sink = _ParquetSink(sections_path, SECTIONS_SCHEMA)
    sections = _FrameChunks(SECTIONS_SCHEMA, sink)

    progress = progress or Progress()
    progress.set_total("versions", len(wanted))
    try:
        for batch in contents:
            for version_id, content in batch.iter_rows():
                meta = wanted.get(version_id)
                if meta is None:
                    continue
                progress.advance("versions")
                if content is None:
                    continue
                artifact_id, repo_id, blob_sha = meta
                version_metrics, document = compute_metrics(content, config.min_chars_for_language)
                shared = outline_by_blob.get(blob_sha)
                if shared is None:
                    shared = outline_by_blob[blob_sha] = outline(document)
                outlines[(repo_id, blob_sha)] = shared
                metrics.add(
                    {"version_id": version_id, "artifact_id": artifact_id, "repo_id": repo_id}
                    | version_metrics.as_row()
                )
                sections.extend(document_section_rows(document, version_id, artifact_id, repo_id))
            clear_syllable_cache()
        sections.flush()
    finally:
        sink.close()
    return metrics.frame(), sections.height, outlines


def document_section_rows(
    document: ParsedDocument, version_id: int, artifact_id: int, repo_id: int
) -> list[dict[str, Any]]:
    """Rows of ``version_sections`` (``SECTIONS_SCHEMA``) for one parsed document."""
    return [
        {
            "version_id": version_id,
            "artifact_id": artifact_id,
            "repo_id": repo_id,
            "position": section.position,
            "level": section.level,
            "heading": section.heading,
            "heading_path": list(section.path),
            "h1": section.ancestor(1),
            "h2": section.ancestor(2),
            "h3": section.ancestor(3),
            "text": section.text,
            "words": count_words(section.text),
        }
        for section in document.sections
    ]


METRICS_SCHEMA = {
    "version_id": pl.Int64,
    "artifact_id": pl.Int64,
    "repo_id": pl.Int64,
    "lines": pl.Int64,
    "chars": pl.Int64,
    "words": pl.Int64,
    "headings": pl.Int64,
    "h1": pl.Int64,
    "h2": pl.Int64,
    "h3": pl.Int64,
    "h4_plus": pl.Int64,
    "max_heading_depth": pl.Int64,
    "sections": pl.Int64,
    "paragraphs": pl.Int64,
    "code_blocks": pl.Int64,
    "code_languages": pl.List(pl.Utf8),
    "inline_code": pl.Int64,
    "bullet_lists": pl.Int64,
    "ordered_lists": pl.Int64,
    "list_items": pl.Int64,
    "task_items": pl.Int64,
    "tables": pl.Int64,
    "links": pl.Int64,
    "images": pl.Int64,
    "blockquotes": pl.Int64,
    "html_blocks": pl.Int64,
    "emphasis": pl.Int64,
    "has_frontmatter": pl.Boolean,
    "frontmatter_valid": pl.Boolean,
    "frontmatter_keys": pl.List(pl.Utf8),
    "frontmatter_json": pl.Utf8,
    "heading_tree_json": pl.Utf8,
    "language": pl.Utf8,
    "flesch_reading_ease": pl.Float64,
    "flesch_kincaid_grade": pl.Float64,
}

SECTIONS_SCHEMA = {
    "version_id": pl.Int64,
    "artifact_id": pl.Int64,
    "repo_id": pl.Int64,
    "position": pl.Int64,
    "level": pl.Int64,
    "heading": pl.Utf8,
    "heading_path": pl.List(pl.Utf8),
    "h1": pl.Utf8,
    "h2": pl.Utf8,
    "h3": pl.Utf8,
    "text": pl.Utf8,
    "words": pl.Int64,
}


# --------------------------------------------------------------------------- gold


def head_versions(tables: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """Artifacts present at the mined HEAD with the id of their HEAD version."""
    artifacts = tables["artifacts"].filter(pl.col("exists_at_head"))
    versions = tables["artifact_versions"].select(
        pl.col("id").alias("version_id"),
        "artifact_id",
        "blob_sha",
        "is_symlink",
        "size_bytes",
        "content_omitted",
    )
    repositories = tables["repositories"].select(pl.col("id").alias("repo_id"), "full_name")
    return (
        artifacts.rename({"id": "artifact_id"})
        .join(
            versions,
            left_on=["artifact_id", "head_blob_sha"],
            right_on=["artifact_id", "blob_sha"],
            how="left",
        )
        .join(repositories, on="repo_id", how="left")
    )


_SIZED_KINDS = (ArtifactKind.INSTRUCTION.value, ArtifactKind.SKILL_MANIFEST.value)
# Contributors of ``rq3_author_roles`` that are people: neither bots nor coding agents.
_HUMAN = ~pl.col("is_bot") & pl.col("agent").is_null()
# The directory of the skill an artifact belongs to (``[<dir>/].claude/skills/<name>``). It
# identifies a skill: ``skill_name`` alone repeats across packages of a monorepo.
SKILL_DIR = pl.col("path").str.extract(r"^((?:.*/)?\.claude/skills/[^/]+)/", 1)


def latest_versions(tables: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """The latest version of every artifact: its HEAD version, or for an artifact deleted
    before HEAD the last version its history recorded."""
    versions = tables["artifact_versions"].select(
        pl.col("id").alias("version_id"), "artifact_id", "blob_sha", "is_symlink", "size_bytes"
    )
    artifacts = tables["artifacts"].rename({"id": "artifact_id"})
    at_head = artifacts.filter(pl.col("exists_at_head")).join(
        versions,
        left_on=["artifact_id", "head_blob_sha"],
        right_on=["artifact_id", "blob_sha"],
        how="left",
    )
    last_recorded = (
        tables["artifact_changes"]
        .filter(pl.col("version_id").is_not_null())
        .join(tables["commits"].select("repo_id", "sha", "committed_at"), on=["repo_id", "sha"])
        .sort("committed_at", "sha")
        .group_by("artifact_id")
        .agg(pl.col("version_id").last())
    )
    deleted = (
        artifacts.filter(~pl.col("exists_at_head"))
        .join(last_recorded, on="artifact_id", how="left")
        .join(versions.drop("artifact_id", "blob_sha"), on="version_id", how="left")
    )
    columns = ["artifact_id", "repo_id", "path", "kind", "skill_name", "exists_at_head"]
    columns += ["version_id", "is_symlink", "size_bytes"]
    return pl.concat([at_head.select(columns), deleted.select(columns)])


def build_artifact_sample(tables: dict[str, pl.DataFrame], config: AnalysisConfig) -> pl.DataFrame:
    """Which artifacts belong to the study sample (``in_sample``).

    Instruction files and skill manifests need ``min_artifact_bytes`` in their latest version
    (a symlink never qualifies by itself; its target is an artifact of its own). Skill resources
    follow the manifest in their own skill directory.
    """
    latest = latest_versions(tables).with_columns(
        SKILL_DIR.alias("skill_dir"),
        (
            pl.col("kind").is_in(_SIZED_KINDS)
            & ~pl.col("is_symlink").fill_null(True)
            & (pl.col("size_bytes").fill_null(0) >= config.min_artifact_bytes)
        ).alias("meets_min_size"),
    )
    skills = (
        latest.filter(pl.col("kind") == ArtifactKind.SKILL_MANIFEST.value)
        .group_by("repo_id", "skill_dir")
        .agg(pl.col("meets_min_size").any().alias("skill_in_sample"))
    )
    return (
        latest.join(skills, on=["repo_id", "skill_dir"], how="left")
        .with_columns(
            pl.when(pl.col("kind") == ArtifactKind.SKILL_RESOURCE.value)
            .then(pl.col("skill_in_sample").fill_null(False))
            .otherwise(pl.col("meets_min_size"))
            .alias("in_sample")
        )
        .drop("skill_in_sample", "version_id")
        .sort("repo_id", "path")
    )


def build_repository_sample(
    tables: dict[str, pl.DataFrame],
    artifact_sample: pl.DataFrame,
    roles: pl.DataFrame,
    config: AnalysisConfig,
) -> pl.DataFrame:
    """Mined repositories with ``in_sample`` and, when excluded, the first failing criterion
    (``exclusion_reason``), continuing the selection funnel on the mined data."""
    sized = artifact_sample.group_by("repo_id").agg(
        (pl.col("in_sample") & pl.col("kind").is_in(_SIZED_KINDS)).sum().alias("sample_artifacts")
    )
    humans = (
        roles.filter(_HUMAN & (pl.col("commits") > 0))
        .group_by("repo_id")
        .agg(pl.len().alias("human_contributors"))
    )
    repositories = (
        tables["repositories"]
        .filter(pl.col("status") == "MINED")
        .select(pl.col("id").alias("repo_id"), "full_name")
        .join(sized, on="repo_id", how="left")
        .join(humans, on="repo_id", how="left")
        .with_columns(pl.col("sample_artifacts", "human_contributors").fill_null(0))
    )
    reason = (
        pl.when(pl.col("sample_artifacts") == 0)
        .then(pl.lit("min_artifact_bytes"))
        .when(pl.col("human_contributors") < config.min_human_contributors)
        .then(pl.lit("min_human_contributors"))
        .otherwise(pl.lit(None, dtype=pl.Utf8))
    )
    return (
        repositories.with_columns(reason.alias("exclusion_reason"))
        .with_columns(pl.col("exclusion_reason").is_null().alias("in_sample"))
        .sort("repo_id")
    )


def sample_artifact_ids(
    artifact_sample: pl.DataFrame, repository_sample: pl.DataFrame
) -> pl.DataFrame:
    """``artifact_id`` of the sample artifacts of the sample repositories."""
    included = repository_sample.filter(pl.col("in_sample")).select("repo_id")
    return (
        artifact_sample.filter(pl.col("in_sample"))
        .join(included, on="repo_id", how="semi")
        .select("artifact_id")
    )


def _rq1_chunks(
    tables: dict[str, pl.DataFrame],
    sections: pl.DataFrame | pl.LazyFrame,
    keep: pl.DataFrame | None = None,
) -> Iterator[pl.DataFrame]:
    """``rq1_sections`` in order, ``_CHUNK_ROWS`` rows at a time (only artifacts in ``keep``,
    when given). ``sections`` may be a scan of ``version_sections``: only HEAD rows are loaded.

    The scan is filtered a slice at a time inside the query: joining it loaded every section of
    every version (gigabytes), a filtered collect of the whole scan buffered far more than it
    kept, and filtering a collected slice keeps the whole slice alive (polars strings share the
    buffers of the frame they were filtered from). The order is computed on the keys alone, and
    the rows are gathered a chunk at a time: sorting or joining the frame copies all the text."""
    heads = head_versions(tables).select(
        "version_id", "artifact_id", "full_name", "path", "tool", "kind", "scope", "skill_name"
    )
    if keep is not None:
        heads = heads.join(keep.select("artifact_id"), on="artifact_id", how="semi")
    at_head = pl.col("version_id").is_in(heads["version_id"].drop_nulls().implode())
    head_rows = pl.concat(
        _scan_slices(sections.lazy(), _SECTIONS_SLICE_ROWS, at_head), how="vertical"
    )
    order = (
        head_rows.select("version_id", "repo_id", "artifact_id", "position")
        .with_row_index("_row")
        .join(heads.drop("artifact_id"), on="version_id", how="inner")
        .sort("repo_id", "artifact_id", "position")
    )
    head_columns = heads.drop("version_id", "artifact_id").columns
    for chunk in order.iter_slices(_CHUNK_ROWS) if order.height else [order]:
        yield head_rows.select(pl.all().gather(chunk["_row"])).hstack(chunk.select(head_columns))


def build_rq1_sections(
    tables: dict[str, pl.DataFrame],
    sections: pl.DataFrame | pl.LazyFrame,
    keep: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Sections of the HEAD versions (of the artifacts in ``keep``, when given)."""
    return pl.concat(_rq1_chunks(tables, sections, keep), how="vertical")


def write_rq1_sections(
    tables: dict[str, pl.DataFrame],
    sections: pl.DataFrame | pl.LazyFrame,
    path: Path,
    keep: pl.DataFrame | None = None,
) -> int:
    """``build_rq1_sections`` written to ``path`` a chunk at a time (never whole in memory)."""
    return _write_chunks(_rq1_chunks(tables, sections, keep), path)


def _write_chunks(
    chunks: Iterable[pl.DataFrame],
    path: Path,
    on_chunk: Callable[[pl.DataFrame], None] | None = None,
) -> int:
    """Write frames of one schema to ``path`` as they come (at least one frame is expected, so
    the file always has the schema); returns the number of rows."""
    sink: _ParquetSink | None = None
    rows = 0
    try:
        for chunk in chunks:
            if sink is None:
                sink = _ParquetSink(path, dict(chunk.schema))
            sink(chunk)
            rows += chunk.height
            if on_chunk is not None:
                on_chunk(chunk)
    finally:
        if sink is not None:
            sink.close()
    return rows


def build_rq2_structure(tables: dict[str, pl.DataFrame], metrics: pl.DataFrame) -> pl.DataFrame:
    heads = head_versions(tables)
    return heads.join(metrics.drop("artifact_id", "repo_id"), on="version_id", how="left").sort(
        "repo_id", "path"
    )


def build_author_roles(tables: dict[str, pl.DataFrame], config: AnalysisConfig) -> pl.DataFrame:
    activity = tables["contributor_activity"]
    changes_by_identity: dict[int, dict[str, int]] = defaultdict(dict)
    for row in activity.iter_rows(named=True):
        changes_by_identity[row["identity_id"]][row["file_category"]] = row["file_changes"]
    artifact_commits = (
        tables["commits"].group_by("author_identity_id").agg(pl.len().alias("artifact_commits"))
    )
    artifact_commit_count = dict(artifact_commits.iter_rows())

    rows = _FrameChunks(
        {
            "identity_id": pl.Int64,
            "repo_id": pl.Int64,
            "name": pl.Utf8,
            "github_login": pl.Utf8,
            "commits": pl.Int64,
            "artifact_commits": pl.Int64,
            "is_bot": pl.Boolean,
            "agent": pl.Utf8,
            "role": pl.Utf8,
            "file_changes": pl.Int64,
            **{f"share_{category.value}": pl.Float64 for category in FileCategory},
        }
    )
    for identity in tables["identities"].iter_rows(named=True):
        file_changes = changes_by_identity.get(identity["id"], {})
        shares = category_shares(file_changes)
        bot = is_bot(identity["name"], identity["email"], config)
        agent = detect_agent(identity["name"], identity["email"], config)
        rows.add(
            {
                "identity_id": identity["id"],
                "repo_id": identity["repo_id"],
                "name": identity["name"],
                "github_login": identity["github_login"],
                "commits": identity["commits"],
                "artifact_commits": artifact_commit_count.get(identity["id"], 0),
                "is_bot": bot,
                "agent": agent,
                "role": author_role(shares, bot, config, agent=agent),
                "file_changes": sum(file_changes.values()),
                **{
                    f"share_{category.value}": shares.get(category.value, 0.0)
                    for category in FileCategory
                },
            }
        )
    return rows.frame()


_CO_CHANGED = ["co_changed_files", *(f"co_changed_{category.value}" for category in FileCategory)]
_CHANGE_FILE_KEY = ["repo_id", "sha", "path"]


def co_changed_counts(
    commit_files: pl.DataFrame | pl.LazyFrame, keys: pl.DataFrame
) -> pl.DataFrame:
    """For each (repo_id, sha, path) of ``keys``: how many other files its commit changed, in
    total and by category. ``commit_files`` has millions of rows: it is aggregated by polars
    (streaming, when it is a scan), never loaded or looped over in Python."""

    def counts(frame: pl.LazyFrame, by: list[str]) -> pl.DataFrame:
        return (
            frame.group_by(by)
            .agg(
                pl.len().cast(pl.Int64).alias("co_changed_files"),
                *(
                    (pl.col("file_category") == category.value)
                    .sum()
                    .cast(pl.Int64)
                    .alias(f"co_changed_{category.value}")
                    for category in FileCategory
                ),
            )
            .collect(engine="streaming")
        )

    files = commit_files.lazy().select(*_CHANGE_FILE_KEY, "file_category")
    keys = keys.select(_CHANGE_FILE_KEY).unique()
    commits = keys.select("repo_id", "sha").unique()
    per_commit = counts(
        files.join(commits.lazy(), on=["repo_id", "sha"], how="semi"), ["repo_id", "sha"]
    )
    own = counts(files.join(keys.lazy(), on=_CHANGE_FILE_KEY, how="semi"), _CHANGE_FILE_KEY)
    return (
        keys.join(per_commit, on=["repo_id", "sha"], how="left")
        .join(own, on=_CHANGE_FILE_KEY, how="left", suffix="_own")
        .select(
            *_CHANGE_FILE_KEY,
            *(
                (pl.col(column).fill_null(0) - pl.col(f"{column}_own").fill_null(0)).alias(column)
                for column in _CO_CHANGED
            ),
        )
    )


def build_rq3_changes(
    tables: dict[str, pl.DataFrame],
    documents: dict[tuple[int, str], ParsedDocument | DocumentOutline],
    roles: pl.DataFrame,
    config: AnalysisConfig,
    commit_files: pl.DataFrame | pl.LazyFrame | None = None,
    keep: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Every artifact change (of the artifacts in ``keep``, when given).

    ``commit_files`` defaults to ``tables["commit_files"]``; the analysis passes a scan
    (``load_bronze`` does not load it). Only the commits of the changes are counted, so files
    of commits removed by the history cut never matter."""
    return pl.concat(
        _rq3_chunks(tables, documents, roles, config, commit_files, keep), how="vertical"
    )


# What ``build_repo_features`` reads of ``rq3_changes`` (all that the analysis keeps of it).
_RQ3_FEATURE_COLUMNS = ("repo_id", "sha", "authored_at", "in_burst", "agent_involved")


def write_rq3_changes(
    tables: dict[str, pl.DataFrame],
    documents: dict[tuple[int, str], ParsedDocument | DocumentOutline],
    roles: pl.DataFrame,
    config: AnalysisConfig,
    path: Path,
    commit_files: pl.DataFrame | pl.LazyFrame | None = None,
    keep: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """``build_rq3_changes`` written to ``path`` a chunk at a time (the commit messages make
    the whole table large). Returns its ``_RQ3_FEATURE_COLUMNS``, for ``build_repo_features``."""
    kept: list[pl.DataFrame] = []
    _write_chunks(
        _rq3_chunks(tables, documents, roles, config, commit_files, keep),
        path,
        lambda chunk: kept.append(chunk.select(_RQ3_FEATURE_COLUMNS)),
    )
    return pl.concat(kept, how="vertical")


def _with_messages(rows: pl.DataFrame, messages: pl.DataFrame) -> pl.DataFrame:
    """``rows`` with the message (and its subject) of their commit, in ``_RQ3_SCHEMA`` order."""
    return (
        rows.join(messages, on=["repo_id", "sha"], how="left", maintain_order="left")
        .with_columns(pl.col("message").fill_null(""))
        .with_columns(
            pl.col("message").str.splitn("\n", 2).struct.field("field_0").alias("message_subject")
        )
        .select(list(_RQ3_SCHEMA))
    )


def _rq3_chunks(
    tables: dict[str, pl.DataFrame],
    documents: dict[tuple[int, str], ParsedDocument | DocumentOutline],
    roles: pl.DataFrame,
    config: AnalysisConfig,
    commit_files: pl.DataFrame | pl.LazyFrame | None,
    keep: pl.DataFrame | None,
) -> Iterator[pl.DataFrame]:
    """``rq3_changes`` in order, ``_CHUNK_ROWS`` rows at a time (at least one, maybe empty)."""
    if commit_files is None:
        commit_files = tables["commit_files"]
    commits = tables["commits"].select(
        "repo_id",
        "sha",
        "authored_at",
        "committed_at",
        "author_identity_id",
        "author_name",
        "author_email",
        "committer_name",
        "committer_email",
    )
    artifacts = tables["artifacts"].select(
        pl.col("id").alias("artifact_id"), "path", "tool", "kind", "scope", "skill_name"
    )
    changes = (
        tables["artifact_changes"]
        .join(commits, on=["repo_id", "sha"], how="inner")
        .join(artifacts, on="artifact_id", how="inner")
    )
    if keep is not None:
        # Whole artifacts are kept or dropped, so the order and timing of the changes of a kept
        # artifact are the same as without the filter.
        changes = changes.join(keep.select("artifact_id"), on="artifact_id", how="semi")
    changes = changes.join(
        co_changed_counts(commit_files, changes), on=_CHANGE_FILE_KEY, how="left"
    ).sort("artifact_id", "authored_at", "committed_at", "sha")

    coauthor_agents: dict[tuple[int, str], set[str]] = defaultdict(set)
    for row in tables["commit_coauthors"].iter_rows(named=True):
        agent = detect_agent(row["name"], row["email"], config)
        if agent:
            coauthor_agents[(row["repo_id"], row["sha"])].add(agent)

    role_by_identity = {
        row[0]: row[1:] for row in roles.select("identity_id", "role", "is_bot").iter_rows()
    }

    # The commit message is joined to each chunk of rows: it is most of the table (a commit that
    # changes several artifacts repeats it), so it never goes through Python rows.
    messages = tables["commits"].select("repo_id", "sha", "message")
    done: list[pl.DataFrame] = []
    rows = _FrameChunks(
        {name: dtype for name, dtype in _RQ3_SCHEMA.items() if name not in _MESSAGE_COLUMNS},
        done.append,
    )
    previous_time: dict[int, Any] = {}
    change_index: dict[int, int] = defaultdict(int)
    for row in changes.iter_rows(named=True):
        artifact_id = row["artifact_id"]
        change_index[artifact_id] += 1
        last = previous_time.get(artifact_id)
        hours = None if last is None else (row["authored_at"] - last).total_seconds() / 3600
        previous_time[artifact_id] = row["authored_at"]

        before = (
            documents.get((row["repo_id"], row["previous_blob_sha"]))
            if row["previous_blob_sha"]
            else None
        )
        after = documents.get((row["repo_id"], row["blob_sha"])) if row["blob_sha"] else None
        comparable = (row["previous_blob_sha"] is None or before is not None) and (
            row["blob_sha"] is None or after is not None
        )
        diff = diff_documents(before, after) if comparable else None

        author_agent = detect_agent(row["author_name"], row["author_email"], config)
        committer_agent = detect_agent(row["committer_name"], row["committer_email"], config)
        agents = sorted(
            coauthor_agents[(row["repo_id"], row["sha"])]
            | {agent for agent in (author_agent, committer_agent) if agent}
        )
        role, bot = role_by_identity.get(row["author_identity_id"], (None, None))
        rows.add(
            {
                "repo_id": row["repo_id"],
                "artifact_id": artifact_id,
                "path": row["path"],
                "tool": row["tool"],
                "kind": row["kind"],
                "scope": row["scope"],
                "skill_name": row["skill_name"],
                "sha": row["sha"],
                "change_type": row["change_type"],
                "source_path": row["source_path"],
                "similarity": row["similarity"],
                "additions": row["additions"],
                "deletions": row["deletions"],
                "authored_at": row["authored_at"],
                "committed_at": row["committed_at"],
                "change_index": change_index[artifact_id],
                "hours_since_previous_change": hours,
                "in_burst": None if hours is None else hours <= config.burst_window_hours,
                "diff_available": diff is not None,
                "sections_added": diff.sections_added if diff else None,
                "sections_removed": diff.sections_removed if diff else None,
                "sections_modified": diff.sections_modified if diff else None,
                "frontmatter_added": diff.frontmatter_added if diff else None,
                "frontmatter_removed": diff.frontmatter_removed if diff else None,
                "frontmatter_modified": diff.frontmatter_modified if diff else None,
                "structural_change": diff.is_structural if diff else None,
                **{column: row[column] for column in _CO_CHANGED},
                "author_identity_id": row["author_identity_id"],
                "author_role": role,
                "author_is_bot": bot,
                "agents_involved": agents,
                "agent_involved": bool(agents),
            }
        )
        while done:
            yield _with_messages(done.pop(0), messages)
    rows.flush()
    while done:
        yield _with_messages(done.pop(0), messages)
    if rows.height == 0:
        yield _with_messages(pl.DataFrame(schema=rows.schema), messages)


_MESSAGE_COLUMNS = ("message_subject", "message")
_RQ3_SCHEMA = {
    "repo_id": pl.Int64,
    "artifact_id": pl.Int64,
    "path": pl.Utf8,
    "tool": pl.Utf8,
    "kind": pl.Utf8,
    "scope": pl.Utf8,
    "skill_name": pl.Utf8,
    "sha": pl.Utf8,
    "change_type": pl.Utf8,
    "source_path": pl.Utf8,
    "similarity": pl.Int64,
    "additions": pl.Int64,
    "deletions": pl.Int64,
    "authored_at": pl.Datetime("us", "UTC"),
    "committed_at": pl.Datetime("us", "UTC"),
    "change_index": pl.Int64,
    "hours_since_previous_change": pl.Float64,
    "in_burst": pl.Boolean,
    "diff_available": pl.Boolean,
    "sections_added": pl.List(pl.Utf8),
    "sections_removed": pl.List(pl.Utf8),
    "sections_modified": pl.List(pl.Utf8),
    "frontmatter_added": pl.List(pl.Utf8),
    "frontmatter_removed": pl.List(pl.Utf8),
    "frontmatter_modified": pl.List(pl.Utf8),
    "structural_change": pl.Boolean,
    "co_changed_files": pl.Int64,
    **{f"co_changed_{category.value}": pl.Int64 for category in FileCategory},
    "author_identity_id": pl.Int64,
    "author_role": pl.Utf8,
    "author_is_bot": pl.Boolean,
    "agents_involved": pl.List(pl.Utf8),
    "agent_involved": pl.Boolean,
    "message_subject": pl.Utf8,
    "message": pl.Utf8,
}


def build_repo_features(
    tables: dict[str, pl.DataFrame],
    structure: pl.DataFrame,
    changes: pl.DataFrame,
    roles: pl.DataFrame,
    repository_sample: pl.DataFrame,
) -> pl.DataFrame:
    """Every mined repository (``in_sample``/``exclusion_reason`` tell whether it belongs to the
    study sample); artifact and evolution attributes cover the sample artifacts only."""
    instruction = pl.col("kind") == ArtifactKind.INSTRUCTION.value
    artifact_features = structure.group_by("repo_id").agg(
        (instruction & (pl.col("tool") == ArtifactTool.CLAUDE.value)).any().alias("has_claude_md"),
        (instruction & (pl.col("tool") == ArtifactTool.AGENTS.value)).any().alias("has_agents_md"),
        instruction.sum().alias("instruction_files"),
        (instruction & (pl.col("scope") == "NESTED")).sum().alias("nested_instruction_files"),
        SKILL_DIR.drop_nulls().n_unique().alias("skills"),
        (pl.col("kind") != ArtifactKind.INSTRUCTION.value).sum().alias("skill_files"),
        pl.col("words").filter(instruction).sum().alias("instruction_words"),
        pl.col("flesch_reading_ease").filter(instruction).mean().alias("instruction_reading_ease"),
    )
    evolution = changes.group_by("repo_id").agg(
        pl.len().alias("artifact_changes"),
        pl.col("sha").n_unique().alias("artifact_commits"),
        pl.col("authored_at").min().alias("first_artifact_change_at"),
        pl.col("authored_at").max().alias("last_artifact_change_at"),
        pl.col("in_burst").mean().alias("artifact_burst_share"),
        pl.col("agent_involved").mean().alias("artifact_agent_share"),
    )
    people = roles.group_by("repo_id").agg(
        pl.col("commits").sum().alias("total_commits"),
        _HUMAN.sum().alias("contributors"),
        pl.col("artifact_commits").gt(0).sum().alias("artifact_authors"),
    )
    active_months = (
        tables["repository_activity"].group_by("repo_id").agg(pl.len().alias("active_months"))
    )
    manual = (
        tables["discovery_hits"]
        .group_by("repo_id")
        .agg(pl.col("query").str.starts_with(_MANUAL_QUERY_PREFIX).all().alias("manual_only"))
    )
    repositories = (
        tables["repositories"].rename({"id": "repo_id"}).filter(pl.col("status") == "MINED")
    )
    features = (
        repositories.join(artifact_features, on="repo_id", how="left")
        .join(evolution, on="repo_id", how="left")
        .join(people, on="repo_id", how="left")
        .join(active_months, on="repo_id", how="left")
        .join(manual, on="repo_id", how="left")
        .join(
            repository_sample.select(
                "repo_id", "sample_artifacts", "human_contributors", "in_sample", "exclusion_reason"
            ),
            on="repo_id",
            how="left",
        )
    )
    return features.with_columns(
        (pl.col("total_commits") / pl.col("active_months")).alias("commits_per_active_month"),
        pl.col("manual_only").fill_null(False),
    ).sort("repo_id")


def _sample_funnel(
    repository_sample: pl.DataFrame, artifact_sample: pl.DataFrame, in_sample: pl.DataFrame
) -> dict[str, Any]:
    excluded = repository_sample.filter(~pl.col("in_sample"))
    return {
        "mined_repositories": repository_sample.height,
        "sample_repositories": repository_sample.height - excluded.height,
        "excluded_repositories": dict(
            sorted(excluded.group_by("exclusion_reason").len().iter_rows())
        ),
        "artifacts": artifact_sample.height,
        "sample_artifacts": in_sample.height,
    }


# --------------------------------------------------------------------------- entry point


def build_datasets(
    bronze_dir: Path,
    output_dir: Path,
    config: AnalysisConfig,
    progress: Progress | None = None,
    *,
    history_cutoff: date | None = None,
) -> dict[str, int]:
    """Write every dataset to ``output_dir``, replacing it only once all files are complete.

    With ``history_cutoff`` the bronze tables are first cut at the end of that date
    (``history_cutoff.cut_history``). The analysis never touches the database, so ``progress``
    is only shown by the terminal that runs it.
    """
    staging = output_dir.with_name(output_dir.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        counts = _build_into(
            bronze_dir, staging, config, progress or Progress("ANALYZE"), history_cutoff
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(output_dir, ignore_errors=True)
    staging.rename(output_dir)
    return counts


_STEPS = (
    "loading the bronze export",
    "parsing artifact versions",
    "classifying authors",
    "selecting the study sample",
    "building RQ2 structure",
    "building RQ1 sections",
    "building RQ3 changes",
    "writing datasets",
)


def _build_into(
    bronze_dir: Path,
    output_dir: Path,
    config: AnalysisConfig,
    progress: Progress,
    history_cutoff: date | None,
) -> dict[str, int]:
    progress.set_total("steps", len(_STEPS))
    steps = iter(_STEPS)

    def step() -> None:
        name = next(steps)
        if name != _STEPS[0]:
            progress.advance("steps")
        progress.set_phase(name)

    step()
    tables = load_bronze(bronze_dir)
    cutoff_stats = None
    if history_cutoff is not None:
        tables, cutoff_stats = cut_history(tables, history_cutoff)
        logger.info("History cut at %s: %s", history_cutoff, cutoff_stats)
    step()
    sections_path = output_dir / f"{SILVER_VERSION_SECTIONS}.parquet"
    metrics, section_rows, documents = build_version_tables(
        tables, read_version_contents(bronze_dir), config, sections_path, progress
    )
    counts = {SILVER_VERSION_SECTIONS: section_rows}

    def write(name: str, frame: pl.DataFrame) -> None:
        # Every output is written as soon as it is built, and the caller drops it unless a
        # later step needs it: holding them all until the end added up to gigabytes.
        frame.write_parquet(output_dir / f"{name}.parquet")
        counts[name] = frame.height
        logger.info("Wrote %s rows to %s", frame.height, name)

    step()
    roles = build_author_roles(tables, config)
    step()
    artifact_sample = build_artifact_sample(tables, config)
    repository_sample = build_repository_sample(tables, artifact_sample, roles, config)
    in_sample = sample_artifact_ids(artifact_sample, repository_sample)
    sample_repos = repository_sample.filter(pl.col("in_sample")).select("repo_id")
    write("silver_artifact_sample", artifact_sample)
    write("rq3_author_roles", roles.join(sample_repos, on="repo_id", how="semi"))

    # Silver tables keep every artifact (sensitivity analyses can change the thresholds);
    # gold tables hold the study sample only.
    step()
    structure = build_rq2_structure(tables, metrics).join(in_sample, on="artifact_id", how="semi")
    write("silver_version_metrics", metrics)
    write("rq2_structure", structure)
    del metrics
    step()
    rq1_rows = write_rq1_sections(
        tables, pl.scan_parquet(sections_path), output_dir / "rq1_sections.parquet", in_sample
    )
    counts["rq1_sections"] = rq1_rows
    logger.info("Wrote %s rows to %s", rq1_rows, "rq1_sections")
    step()
    path = output_dir / "rq3_changes.parquet"
    commit_files = scan_bronze(bronze_dir, "commit_files")
    changes = write_rq3_changes(tables, documents, roles, config, path, commit_files, in_sample)
    del documents
    counts["rq3_changes"] = changes.height
    logger.info("Wrote %s rows to %s", changes.height, "rq3_changes")
    step()
    write(
        "repo_features",
        build_repo_features(tables, structure, changes, roles, repository_sample),
    )
    # The analysis never touches the database, so its provenance lives next to its outputs.
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "code_version": code_version(),
        "bronze_dir": str(bronze_dir),
        "bronze_manifest": read_manifest(bronze_dir),
        "analysis_config": config.model_dump(mode="json"),
        # None when the history was not cut; otherwise the date and what the cut removed.
        "history_cutoff": cutoff_stats,
        "sample": _sample_funnel(repository_sample, artifact_sample, in_sample),
        "outputs": counts,
    }
    (output_dir / ANALYSIS_MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    progress.advance("steps")
    return counts
