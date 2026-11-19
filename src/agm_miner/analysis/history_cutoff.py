"""Cut the mined history at the study's reference date (``analysis.truncate_at_reference_date``).

Repositories are mined over weeks, so each clone shows its repository on a different day. Before
the datasets are built, the bronze tables are rewritten as if every repository had been mined at
the end of ``selection.reference_date`` (UTC):

* commits committed after the cutoff are dropped, with their artifact changes, files and
  co-authors;
* an artifact changed after the cutoff takes the state of its last change before it (deleted if
  that change is a deletion); an artifact whose every change is after the cutoff did not exist
  yet and is dropped. An artifact without changes after the cutoff keeps its mined HEAD state,
  which also covers the few artifacts whose history has no recorded change (made only in merge
  commits, which the miner does not diff);
* versions referenced only by dropped changes are dropped;
* identities whose first commit is after the cutoff, and who authored no remaining commit, are
  dropped with their activity; monthly activity stops at the month of the cutoff.

``artifacts.exists_at_head`` and ``head_blob_sha`` then describe the artifact at the cutoff.

What cannot be cut (report it as a limitation): ``identities.commits`` and
``contributor_activity`` aggregate the whole history of the clone, and the repository metadata
(stars, license, ...) is from the selection run. Commits are dated by ``committed_at``, so a
commit made before the cutoff but merged into the default branch after it is kept.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import polars as pl

_COMMIT_KEY = ["repo_id", "sha"]
_VERSION_KEY = ["artifact_id", "blob_sha"]


def cutoff_instant(reference: date) -> datetime:
    """The first instant after ``reference`` (the cut keeps everything before it)."""
    return datetime.combine(reference + timedelta(days=1), time.min, tzinfo=UTC)


def _refs(frame: pl.DataFrame, column: str) -> pl.DataFrame:
    return (
        frame.select("artifact_id", pl.col(column).alias("blob_sha"))
        .drop_nulls("blob_sha")
        .unique()
    )


def cut_history(
    tables: Mapping[str, pl.DataFrame], reference: date
) -> tuple[dict[str, pl.DataFrame], dict[str, Any]]:
    """The tables as of the end of ``reference`` and what the cut removed.

    ``commits``, ``artifact_changes`` and ``artifacts`` are required; the other bronze tables are
    cut when present and passed through otherwise (the audit loads only some of them)."""
    end = cutoff_instant(reference)
    result = dict(tables)
    commits = tables["commits"]
    late_commits = commits.filter(pl.col("committed_at") >= end).select(_COMMIT_KEY)
    kept_commits = commits.join(late_commits, on=_COMMIT_KEY, how="anti")

    dated = tables["artifact_changes"].join(
        commits.select(*_COMMIT_KEY, "committed_at", "authored_at"), on=_COMMIT_KEY, how="left"
    )
    # A change whose commit is unknown cannot be dated and is kept.
    is_late = (pl.col("committed_at") >= end).fill_null(False)
    late_changes, kept_changes = dated.filter(is_late), dated.filter(~is_late)
    changed_late = late_changes.select("artifact_id").unique()

    artifacts = tables["artifacts"]
    untouched = artifacts.join(changed_late, left_on="id", right_on="artifact_id", how="anti")
    affected = artifacts.join(changed_late, left_on="id", right_on="artifact_id", how="semi")
    last_before = (
        kept_changes.join(changed_late, on="artifact_id", how="semi")
        .sort("committed_at", "authored_at", "sha")
        .group_by("artifact_id")
        .agg(pl.col("change_type").last(), pl.col("blob_sha").last())
    )
    exists = pl.col("change_type") != "D"
    reconstructed = affected.join(
        last_before, left_on="id", right_on="artifact_id", how="inner"
    ).with_columns(
        exists.alias("exists_at_head"),
        pl.when(exists).then(pl.col("blob_sha")).otherwise(None).alias("head_blob_sha"),
    )
    not_yet = affected.join(last_before, left_on="id", right_on="artifact_id", how="anti")
    new_artifacts = pl.concat([untouched, reconstructed.select(artifacts.columns)]).sort("id")
    before = affected.select("id", "exists_at_head", "head_blob_sha")
    compared = reconstructed.select("id", "exists_at_head", "head_blob_sha").join(
        before, on="id", suffix="_mined"
    )
    state_changed = compared.filter(
        (pl.col("exists_at_head") != pl.col("exists_at_head_mined"))
        | (pl.col("head_blob_sha").fill_null("") != pl.col("head_blob_sha_mined").fill_null(""))
    )
    removed = compared.filter(~pl.col("exists_at_head") & pl.col("exists_at_head_mined"))

    result["commits"] = kept_commits
    result["artifact_changes"] = kept_changes.drop("committed_at", "authored_at")
    result["artifacts"] = new_artifacts
    versions_dropped = 0
    if "artifact_versions" in tables:
        versions = tables["artifact_versions"]
        kept_refs = pl.concat(
            [
                _refs(kept_changes, "blob_sha"),
                _refs(kept_changes, "previous_blob_sha"),
                _refs(new_artifacts.rename({"id": "artifact_id"}), "head_blob_sha"),
            ]
        ).unique()
        dropped_refs = _refs(late_changes, "blob_sha").join(kept_refs, on=_VERSION_KEY, how="anti")
        cut_versions = versions.join(dropped_refs, on=_VERSION_KEY, how="anti").join(
            new_artifacts.select(pl.col("id").alias("artifact_id")), on="artifact_id", how="semi"
        )
        versions_dropped = versions.height - cut_versions.height
        result["artifact_versions"] = cut_versions
    for name in ("commit_files", "commit_coauthors"):
        if name in tables:
            result[name] = tables[name].join(late_commits, on=_COMMIT_KEY, how="anti")
    identities_dropped = 0
    if "identities" in tables:
        identities = tables["identities"]
        authors = kept_commits.select(pl.col("author_identity_id").alias("id")).unique()
        late_people = identities.filter(pl.col("first_commit_at") >= end).join(
            authors, on="id", how="anti"
        )
        result["identities"] = identities.join(late_people, on="id", how="anti")
        identities_dropped = late_people.height
        if "contributor_activity" in tables:
            result["contributor_activity"] = tables["contributor_activity"].join(
                late_people.select(pl.col("id").alias("identity_id")), on="identity_id", how="anti"
            )
    if "repository_activity" in tables:
        month = date(reference.year, reference.month, 1)
        result["repository_activity"] = tables["repository_activity"].filter(
            pl.col("month") <= month
        )

    stats = {
        "reference_date": reference.isoformat(),
        "cutoff": end.isoformat(),
        "commits_after_cutoff": late_commits.height,
        "repositories_with_commits_after_cutoff": late_commits["repo_id"].n_unique(),
        "artifact_changes_after_cutoff": late_changes.height,
        "artifacts_changed_after_cutoff": changed_late.height,
        "artifacts_not_yet_created": not_yet.height,
        "artifacts_with_another_state": state_changed.height,
        "artifacts_deleted_at_cutoff": removed.height,
        "versions_dropped": versions_dropped,
        "identities_dropped": identities_dropped,
    }
    return result, stats
