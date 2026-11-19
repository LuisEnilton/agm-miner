"""The data dictionary (data_dictionary.yaml) and its rendering in docs/dados.md."""

from __future__ import annotations

from pathlib import Path

import pytest

from agm_miner.replication.data_dictionary import (
    load_dictionary,
    parse_dictionary,
    render_markdown,
    undocumented_columns,
)

ROOT = Path(__file__).resolve().parents[1]


def test_docs_dados_is_the_rendered_dictionary() -> None:
    expected = render_markdown()
    assert (ROOT / "docs" / "dados.md").read_text(encoding="utf-8") == expected, (
        "docs/dados.md is out of date: python -m agm_miner.replication.data_dictionary "
        "> docs/dados.md"
    )


def test_every_table_and_column_has_a_description() -> None:
    dictionary = load_dictionary()
    assert {table.layer for table in dictionary.tables} == {"bronze", "silver", "gold"}
    for table in dictionary.tables:
        assert table.description and table.columns, table.name
        assert all(text for _, text in table.columns), table.name


def test_personal_data_columns_are_marked() -> None:
    dictionary = load_dictionary()
    personal = {
        (table.name, column)
        for table in dictionary.tables
        for column, text in table.columns
        if "Dado pessoal" in text
    }
    for expected in [
        ("commits", "author_email"),
        ("commits", "committer_name"),
        ("identities", "email"),
        ("identities", "identity_key"),
        ("commit_coauthors", "email"),
        ("rq3_author_roles", "name"),
        ("rq3_author_roles", "github_login"),
    ]:
        assert expected in personal


def test_defaults_fill_columns_and_missing_descriptions_fail() -> None:
    raw = {
        "columns": {"repo_id": "Repository."},
        "tables": {"t": {"layer": "gold", "description": "T.", "columns": {"repo_id": None}}},
    }
    assert parse_dictionary(raw).table("t").columns == (("repo_id", "Repository."),)
    raw["tables"]["t"]["columns"]["other"] = None
    with pytest.raises(ValueError, match="other has no description"):
        parse_dictionary(raw)
    raw["tables"]["t"] = {"layer": "platinum", "description": "T.", "columns": {}}
    with pytest.raises(ValueError, match="layer"):
        parse_dictionary(raw)


def test_undocumented_columns_reports_both_directions() -> None:
    missing, extra = undocumented_columns("commit_files", ["repo_id", "sha", "path", "new"])
    assert missing == ["new"] and extra == ["change_type", "file_category"]
