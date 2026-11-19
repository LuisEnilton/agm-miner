"""Data dictionary of the bronze, silver and gold tables (single source: data_dictionary.yaml).

``python -m agm_miner.replication.data_dictionary > docs/dados.md`` renders it; the replication
package ships the same text. :func:`undocumented_columns` lets the tests check that every column
the export and the analysis write is described.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

DICTIONARY_PATH = Path(__file__).with_name("data_dictionary.yaml")
LAYERS = ("bronze", "silver", "gold")


@dataclass(frozen=True, slots=True)
class Table:
    name: str
    layer: str
    description: str
    columns: tuple[tuple[str, str], ...]  # (column, description), in Parquet order


@dataclass(frozen=True, slots=True)
class DataDictionary:
    layers: Mapping[str, str]
    tables: tuple[Table, ...]

    def table(self, name: str) -> Table:
        for table in self.tables:
            if table.name == name:
                return table
        raise KeyError(name)


def parse_dictionary(raw: Mapping) -> DataDictionary:
    defaults = raw.get("columns") or {}
    tables = []
    for name, spec in (raw.get("tables") or {}).items():
        if spec.get("layer") not in LAYERS:
            raise ValueError(f"table {name}: layer must be one of {LAYERS}")
        columns = []
        for column, description in (spec.get("columns") or {}).items():
            text = description or defaults.get(column)
            if not text:
                raise ValueError(f"table {name}: column {column} has no description")
            columns.append((column, " ".join(str(text).split())))
        description = " ".join(spec["description"].split())
        tables.append(Table(name, spec["layer"], description, tuple(columns)))
    return DataDictionary(dict(raw.get("layers") or {}), tuple(tables))


@lru_cache
def load_dictionary(path: Path = DICTIONARY_PATH) -> DataDictionary:
    return parse_dictionary(yaml.safe_load(path.read_text(encoding="utf-8")))


def undocumented_columns(
    table: str, columns: Iterable[str], dictionary: DataDictionary | None = None
) -> tuple[list[str], list[str]]:
    """(columns missing from the dictionary, documented columns the table does not have)."""
    documented = [column for column, _ in (dictionary or load_dictionary()).table(table).columns]
    actual = list(columns)
    return (
        [column for column in actual if column not in documented],
        [column for column in documented if column not in actual],
    )


def render_markdown(dictionary: DataDictionary | None = None) -> str:
    dictionary = dictionary or load_dictionary()
    lines = [
        "# Dicionário de dados",
        "",
        "<!-- Gerado de src/agm_miner/replication/data_dictionary.yaml: não edite à mão. Para",
        "     atualizar: python -m agm_miner.replication.data_dictionary > docs/dados.md -->",
        "",
        "Todas as tabelas são arquivos Parquet. As camadas seguem o fluxo do pipeline:",
        "",
    ]
    for layer in LAYERS:
        lines.append(f"- **{layer}**: {' '.join(dictionary.layers.get(layer, '').split())}")
    lines += [
        "",
        "Colunas marcadas como *dado pessoal* são pseudonimizadas no pacote de replicação.",
        "",
    ]
    for layer in LAYERS:
        tables = [table for table in dictionary.tables if table.layer == layer]
        lines += [f"## {layer.capitalize()}", ""]
        lines += [f"- [`{table.name}`](#{table.name})" for table in tables]
        lines.append("")
        for table in tables:
            lines += [
                f'<a id="{table.name}"></a>',
                "",
                f"### `{table.name}`",
                "",
                table.description,
                "",
                "| Coluna | Descrição |",
                "|---|---|",
            ]
            lines += [f"| `{column}` | {_cell(text)} |" for column, text in table.columns]
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


if __name__ == "__main__":
    print(render_markdown(), end="")
