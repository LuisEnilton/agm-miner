"""Replication package: the published data, results and configuration of one export, ready
for Zenodo (``agm package``).

Reads only files (the bronze export, its analysis, the RQ1 folders and the result folders), never
the database. Personal data are pseudonymised on the way (:mod:`.anonymize`); the files of the
Agent READMEs dataset are not redistributed (it declares no licence): ``reproduzir.sh`` downloads
them from pinned URLs and checks their SHA-256. Everything goes to ``<output>.partial`` and is
renamed at the end, like the export and the analysis.

Layout (every zip mirrors ``DATA_DIR``, so unzipping them all into one folder gives a
``DATA_DIR`` the commands read directly)::

    README.md  LICENSE-DATA.md  DICIONARIO_DE_DADOS.md  manifest.json  SHA256SUMS
    reproduzir.sh  licencas.csv
    config/          study.yaml, codebooks/, fontes_agentreadmes.tsv
    dados/           bronze-<export>.zip, gold-<export>.zip
    rq1/             rq1-<export>.zip, rq1-agentreadmes.zip (cache of the LLM answers + outputs)
    resultados/      resultados-<export>.zip (audit, rq2, figures) + the main files unzipped
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow.parquet as pq

from agm_miner.progress import Progress
from agm_miner.replication.anonymize import (
    PERSONAL_COLUMNS,
    Pseudonymizer,
    anonymize_frame,
    has_personal_data,
)
from agm_miner.replication.data_dictionary import render_markdown

CHUNK_ROWS = 50_000
# Reproducible zips: the same inputs give the same bytes.
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
# Operational files of the RQ1 folders that a replication must not inherit: a batch still listed
# as pending would make `classify` (without --offline) wait for it; locks belong to a process.
_SKIPPED_NAMES = frozenset({"batches.json"})
_SKIPPED_SUFFIXES = (".lock", ".tmp")
# Main result files copied unzipped too, so they can be read on Zenodo without downloading.
_BROWSABLE = ("audit.md", "audit.tex", "audit_rascunho.tex", "rq2_summary.md", "figuras.tex")

TOOL_REPOSITORY = "https://github.com/LuisEnilton/agm-miner"
AGENT_READMES = "agentreadmes"
# Sources of the external validation, pinned to the revisions used (docs/rq1.md).
AGENT_READMES_SOURCES = {
    "dataset": (
        "train-00000-of-00001.parquet",
        "https://huggingface.co/datasets/hao-li/AgentREADMEs/resolve/"
        "e5842f4eceb11e61f247212b04b692bfb397f8b7/data/train-00000-of-00001.parquet",
    ),
    "human_labels": (
        "final_am_classification_3.csv",
        "https://raw.githubusercontent.com/woraamy/Agent-Context-File-Analysis/"
        "77b16a9a53c99efcda1c98c98c4afcd807f7227c/datasets/derived/classification/inputs/"
        "final_am_classification_3.csv",
    ),
    "gpt5_labels": (
        "gpt_5_file_classifications.csv",
        "https://raw.githubusercontent.com/woraamy/Agent-Context-File-Analysis/"
        "77b16a9a53c99efcda1c98c98c4afcd807f7227c/datasets/derived/classification/inputs/"
        "gpt_5_file_classifications.csv",
    ),
}


@dataclass(frozen=True, slots=True)
class ToolReference:
    """The released version of the tool that reproduces the package (cited in its README)."""

    version: str
    doi: str | None = None

    @property
    def tag(self) -> str:
        return f"v{self.version}"


class PackageInputError(RuntimeError):
    """An input of the package is missing, the salt is not set or the output already exists."""


@dataclass(frozen=True, slots=True)
class PackageInputs:
    bronze_dir: Path
    gold_dir: Path
    data_dir: Path
    study_path: Path
    codebooks_dir: Path
    output_dir: Path

    @property
    def name(self) -> str:
        return self.bronze_dir.name

    def rq1_dirs(self) -> list[Path]:
        candidates = [self.data_dir / "rq1" / self.name, self.data_dir / "rq1" / AGENT_READMES]
        return [path for path in candidates if path.is_dir()]

    def result_dirs(self) -> list[Path]:
        """audit/<export>, rq2/<export>*, figures/<export> that exist."""
        found = []
        for kind in ("audit", "rq2", "figures"):
            parent = self.data_dir / kind
            if parent.is_dir():
                found += sorted(
                    path
                    for path in parent.iterdir()
                    if path.is_dir()
                    and (path.name == self.name or path.name.startswith(f"{self.name}__"))
                )
        return found


def build_package(
    inputs: PackageInputs,
    pseudonymizer: Pseudonymizer,
    *,
    code_version: str,
    tool: ToolReference,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Write the replication package of ``inputs`` and return its summary."""
    progress = progress or Progress()
    _check(inputs)
    final = inputs.output_dir
    partial = final.with_name(f"{final.name}.partial")
    if partial.exists():
        shutil.rmtree(partial)
    work = partial / "_work"
    try:
        progress.set_phase("pseudonymising the bronze export")
        bronze = _copy_tables(inputs.bronze_dir, work / "bronze" / inputs.name, pseudonymizer)
        progress.set_phase("pseudonymising the analysis")
        gold = _copy_tables(inputs.gold_dir, work / "gold" / inputs.name, pseudonymizer)

        progress.set_phase("writing the archives")
        _zip_tree(work / "bronze", partial / "dados" / f"bronze-{inputs.name}.zip")
        _zip_tree(work / "gold", partial / "dados" / f"gold-{inputs.name}.zip")
        shutil.rmtree(work)
        for rq1 in inputs.rq1_dirs():
            _zip_tree(rq1, partial / "rq1" / f"rq1-{rq1.name}.zip", prefix=f"rq1/{rq1.name}")
        results = inputs.result_dirs()
        if results:
            archive = partial / "resultados" / f"resultados-{inputs.name}.zip"
            _zip_paths(archive, [(path, path.relative_to(inputs.data_dir)) for path in results])
            for path in results:
                for name in _BROWSABLE:
                    if (path / name).is_file():
                        shutil.copyfile(path / name, partial / "resultados" / name)

        progress.set_phase("writing the documentation")
        _write_config(inputs, partial / "config")
        licences = _write_licences(inputs.bronze_dir, partial / "licencas.csv")
        (partial / "DICIONARIO_DE_DADOS.md").write_text(render_markdown(), encoding="utf-8")
        (partial / "LICENSE-DATA.md").write_text(LICENSE_DATA, encoding="utf-8")
        script = partial / "reproduzir.sh"
        script.write_text(REPRODUCE_SCRIPT.replace("@EXPORT@", inputs.name), encoding="utf-8")
        script.chmod(0o755)
        manifest = _manifest(inputs, bronze, gold, licences, code_version, tool)
        _write_json(partial / "manifest.json", manifest)
        (partial / "README.md").write_text(_readme(inputs, manifest, partial), encoding="utf-8")
        sums = _write_checksums(partial)
        if final.exists():
            raise PackageInputError(f"{final} already exists; remove it or choose --output")
        partial.rename(final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return {"output": str(final), "files": len(sums), **manifest["contents"]}


# --------------------------------------------------------------------------- inputs


def _check(inputs: PackageInputs) -> None:
    for path, what in (
        (inputs.bronze_dir / "manifest.json", "bronze export"),
        (inputs.gold_dir / "analysis_manifest.json", "analysis of that export (agm analyze)"),
        (inputs.study_path, "study configuration"),
        (inputs.codebooks_dir, "codebooks folder"),
    ):
        if not path.exists():
            raise PackageInputError(f"missing {what}: {path}")
    if inputs.output_dir.exists():
        raise PackageInputError(f"{inputs.output_dir} already exists; remove it or use --output")


# --------------------------------------------------------------------------- tables


def _copy_tables(source: Path, destination: Path, pseudonymizer: Pseudonymizer) -> dict[str, int]:
    """Every file of ``source``: Parquet tables with personal data rewritten in slices (the
    content of the versions never sits whole in memory), the others copied byte for byte."""
    destination.mkdir(parents=True)
    rows = {}
    for path in sorted(source.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        target = destination / path.name
        if path.suffix == ".parquet" and has_personal_data(path.stem):
            rows[path.stem] = _rewrite_parquet(path, target, path.stem, pseudonymizer)
        else:
            shutil.copyfile(path, target)
            if path.suffix == ".parquet":
                rows[path.stem] = pq.ParquetFile(path).metadata.num_rows
    return rows


def _rewrite_parquet(source: Path, target: Path, table: str, p: Pseudonymizer) -> int:
    schema = pq.read_schema(source)
    total = pq.ParquetFile(source).metadata.num_rows
    with pq.ParquetWriter(target, schema) as writer:
        for offset in range(0, max(total, 1), CHUNK_ROWS):
            frame = pl.scan_parquet(source).slice(offset, CHUNK_ROWS).collect()
            writer.write_table(anonymize_frame(table, frame, p).to_arrow().cast(schema))
    return total


# --------------------------------------------------------------------------- archives


def _included(path: Path) -> bool:
    return path.name not in _SKIPPED_NAMES and not path.name.endswith(_SKIPPED_SUFFIXES)


def _zip_tree(source: Path, archive: Path, prefix: str | None = None) -> None:
    """``source`` as ``archive``, with paths under ``prefix`` (default: the source's name)."""
    _zip_paths(archive, [(source, Path(prefix or source.name))])


def _zip_paths(archive: Path, roots: Iterable[tuple[Path, Path]]) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as zipped:
        for root, arcroot in roots:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or not _included(path):
                    continue
                info = zipfile.ZipInfo(str(arcroot / path.relative_to(root)), _ZIP_TIME)
                # Parquet is already compressed; JSON, CSV and LaTeX shrink a lot.
                stored = path.suffix == ".parquet"
                info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                with path.open("rb") as handle, zipped.open(info, "w") as out:
                    shutil.copyfileobj(handle, out, length=1 << 20)


# --------------------------------------------------------------------------- documentation


def _write_config(inputs: PackageInputs, config: Path) -> None:
    (config / "codebooks").mkdir(parents=True)
    shutil.copyfile(inputs.study_path, config / "study.yaml")
    for codebook in sorted(inputs.codebooks_dir.glob("*.yaml")):
        shutil.copyfile(codebook, config / "codebooks" / codebook.name)
    expected = _agentreadmes_checksums(inputs.data_dir)
    with (config / "fontes_agentreadmes.tsv").open("w", encoding="utf-8") as out:
        out.write("arquivo\tsha256\turl\n")
        for key, (filename, url) in AGENT_READMES_SOURCES.items():
            out.write(f"{filename}\t{expected.get(key, '')}\t{url}\n")


def _agentreadmes_checksums(data_dir: Path) -> dict[str, str]:
    manifest = data_dir / "external" / AGENT_READMES / "analysis_manifest.json"
    if not manifest.is_file():
        return {}
    inputs = json.loads(manifest.read_text(encoding="utf-8")).get("inputs", {})
    return {key: value.get("sha256", "") for key, value in inputs.items() if value}


def _write_licences(bronze_dir: Path, target: Path) -> int:
    """repo_id, full_name, license_spdx and URL of every repository whose content is published:
    each artifact version stays under its repository's licence."""
    with_content = (
        pl.scan_parquet(bronze_dir / "artifact_versions.parquet").select("repo_id").unique()
    )
    repositories = (
        pl.scan_parquet(bronze_dir / "repositories.parquet")
        .select(pl.col("id").alias("repo_id"), "full_name", "license_spdx")
        .join(with_content, on="repo_id")
        .sort("repo_id")
        .collect()
    )
    with target.open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(["repo_id", "full_name", "license_spdx", "url"])
        for repo_id, full_name, licence in repositories.iter_rows():
            writer.writerow([repo_id, full_name, licence or "", f"https://github.com/{full_name}"])
    return repositories.height


def _manifest(
    inputs: PackageInputs,
    bronze_rows: dict[str, int],
    gold_rows: dict[str, int],
    licences: int,
    code_version: str,
    tool: ToolReference,
) -> dict[str, Any]:
    bronze = json.loads((inputs.bronze_dir / "manifest.json").read_text(encoding="utf-8"))
    gold = json.loads((inputs.gold_dir / "analysis_manifest.json").read_text(encoding="utf-8"))
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "code_version": code_version,
        "tool": {
            "repository": TOOL_REPOSITORY,
            "version": tool.version,
            "tag": tool.tag,
            "doi": tool.doi,
        },
        "export": inputs.name,
        "bronze": {
            key: bronze.get(key)
            for key in ("exported_at", "export_run_id", "code_version", "schema_revision")
        }
        | {"study_config_hash": bronze.get("study_config_hash")},
        "analysis": {key: gold.get(key) for key in ("created_at", "code_version", "history_cutoff")}
        | {"sample": gold.get("sample")},
        "contents": {
            "bronze_tables": bronze_rows,
            "gold_tables": gold_rows,
            "rq1_folders": [path.name for path in inputs.rq1_dirs()],
            "result_folders": [str(p.relative_to(inputs.data_dir)) for p in inputs.result_dirs()],
            "repositories_with_content": licences,
        },
        "pseudonymised": {table: list(columns) for table, columns in PERSONAL_COLUMNS.items()},
        "not_redistributed": {
            "agentreadmes": "files of the Agent READMEs dataset (no licence declared); "
            "reproduzir.sh downloads them (config/fontes_agentreadmes.tsv)"
        },
    }


def _write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def _write_checksums(root: Path) -> list[str]:
    lines = [
        f"{_sha256(path)}  {path.relative_to(root)}"
        for path in _files(root)
        if path.name != "SHA256SUMS"
    ]
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


def _size(path: Path) -> str:
    size = float(path.stat().st_size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _readme(inputs: PackageInputs, manifest: dict[str, Any], root: Path) -> str:
    rows = "\n".join(
        f"| `{path.relative_to(root)}` | {_size(path)} |"
        for path in _files(root)
        if path.name not in ("README.md", "SHA256SUMS")
    )
    sample = manifest["analysis"].get("sample") or {}
    replacements: dict[str, Callable[[], str]] = {
        "@EXPORT@": lambda: inputs.name,
        "@TOOL@": lambda: _tool_line(manifest["tool"]),
        "@TOOL_TAG@": lambda: str(manifest["tool"]["tag"]),
        "@CODE_VERSION@": lambda: str(manifest["code_version"]),
        "@BRONZE_CODE_VERSION@": lambda: str(manifest["bronze"].get("code_version")),
        "@ANALYSIS_CODE_VERSION@": lambda: str(manifest["analysis"].get("code_version")),
        "@EXPORTED_AT@": lambda: str(manifest["bronze"].get("exported_at")),
        "@SAMPLE@": lambda: json.dumps(sample, ensure_ascii=False, sort_keys=True),
        "@FILES@": lambda: rows,
        "@PERSONAL@": lambda: "\n".join(
            f"- `{table}`: {', '.join(f'`{c}`' for c in columns)}"
            for table, columns in PERSONAL_COLUMNS.items()
        ),
    }
    text = README_TEMPLATE
    for marker, value in replacements.items():
        text = text.replace(marker, value())
    return text


def _tool_line(tool: dict[str, Any]) -> str:
    """The tool version to install, with its DOI when known."""
    url = f"{tool['repository']}/tree/{tool['tag']}"
    doi = f", DOI [{tool['doi']}](https://doi.org/{tool['doi']})" if tool.get("doi") else ""
    return f"`agm-miner` {tool['tag']} ([{url}]({url}){doi})"


LICENSE_DATA = """# Licença dos dados

Os dados, os resultados e a documentação deste pacote são distribuídos sob a licença
[Creative Commons Atribuição 4.0 Internacional (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/deed.pt-br),
com uma exceção:

- **O conteúdo dos artefatos minerados** (colunas `content` de `artifact_versions` e `text` das
  tabelas de seções) e as mensagens de commit vêm de repositórios públicos do GitHub e continuam
  sob a licença de cada repositório de origem. Só foram coletados repositórios com licença aprovada
  pela OSI; a licença de cada um está em `licencas.csv` (junte pelo `repo_id`). Ao reutilizar esse
  conteúdo, siga a licença do repositório e cite a origem.

O código da ferramenta que gerou os dados (`agm-miner`) é distribuído sob a licença MIT, no
repositório e no registro dela no Zenodo.
"""

REPRODUCE_SCRIPT = r"""#!/usr/bin/env bash
# Reproduz os resultados do artigo a partir deste pacote (ver README.md).
#   ./reproduzir.sh L1   dos resultados publicados (minutos; sem banco e sem chave de API)
#   ./reproduzir.sh L2   refaz antes o agm analyze do export bronze (1h30; 4 GB de RAM livres)
# Precisa do comando `agm` da versão citada no README (com o Poetry:
# AGM="poetry -C /caminho/da/ferramenta run agm"),
# de unzip, curl e sha256sum. As saídas vão para $DATA_DIR (padrão: ./data, ao lado do script).
set -euo pipefail

LEVEL=${1:-L1}
HERE=$(cd "$(dirname "$0")" && pwd)
AGM=${AGM:-agm}
EXPORT=@EXPORT@
export DATA_DIR=${DATA_DIR:-$HERE/data}
export STUDY_CONFIG_PATH=$HERE/config/study.yaml
export AGM_PROGRESS=${AGM_PROGRESS:-plain}

echo "== Extraindo os dados em $DATA_DIR"
mkdir -p "$DATA_DIR"
for archive in "$HERE"/dados/*.zip "$HERE"/rq1/*.zip "$HERE"/resultados/*.zip; do
  unzip -oq "$archive" -d "$DATA_DIR"
done
# Os resultados publicados ficam guardados para comparação; os comandos abaixo os refazem.
mkdir -p "$DATA_DIR/publicado"
for kind in audit rq2 figures; do
  if [ -d "$DATA_DIR/$kind" ]; then
    rm -rf "$DATA_DIR/publicado/$kind"
    mv "$DATA_DIR/$kind" "$DATA_DIR/publicado/"
  fi
done

cd "$HERE/config"  # o study.yaml aponta para codebooks/ relativo a esta pasta

if [ "$LEVEL" = "L2" ]; then
  echo "== L2: agm analyze a partir do export bronze"
  mv "$DATA_DIR/gold/$EXPORT" "$DATA_DIR/publicado/gold-$EXPORT"
  $AGM analyze --bronze "$DATA_DIR/bronze/$EXPORT"
fi

echo "== RQ1: classificação a partir das respostas guardadas (--offline)"
$AGM rq1 classify --offline --gold "$DATA_DIR/gold/$EXPORT" \
  --kind INSTRUCTION --kind SKILL_MANIFEST

echo "== Validação externa: fontes do Agent READMEs (baixadas, não redistribuídas)"
SOURCES=$DATA_DIR/sources/agentreadmes
mkdir -p "$SOURCES"
tail -n +2 fontes_agentreadmes.tsv | while IFS=$'\t' read -r file sha url; do
  [ -f "$SOURCES/$file" ] || curl -fsSL -o "$SOURCES/$file" "$url"
  if [ -n "$sha" ]; then echo "$sha  $SOURCES/$file" | sha256sum -c --quiet -; fi
done
$AGM rq1 agentreadmes import "$SOURCES/train-00000-of-00001.parquet" \
  --human-labels "$SOURCES/final_am_classification_3.csv" \
  --gpt5-labels "$SOURCES/gpt_5_file_classifications.csv" --force
$AGM rq1 classify --offline --gold "$DATA_DIR/external/agentreadmes" \
  --codebook codebooks/seed_agent_readmes.yaml --only-labelled
$AGM rq1 agentreadmes score

echo "== RQ2, auditoria e figuras"
$AGM rq2 summary --gold "$DATA_DIR/gold/$EXPORT" --compare "$DATA_DIR/external/agentreadmes"
$AGM audit --bronze "$DATA_DIR/bronze/$EXPORT"
$AGM figures --gold "$DATA_DIR/gold/$EXPORT"

echo "== Pronto. Compare $DATA_DIR/{audit,rq2,figures} com $DATA_DIR/publicado/"
"""

README_TEMPLATE = """# Pacote de replicação: artefatos de configuração de agentes de IA no GitHub

Dados, resultados e configuração do estudo *O que os desenvolvedores dizem aos seus agentes de
código? Um estudo empírico do conteúdo e da estrutura de `CLAUDE.md`, `AGENTS.md` e Skills*,
gerados pela ferramenta `agm-miner` a partir do export `@EXPORT@`.

**Ferramenta para reproduzir:** @TOOL@.

## Proveniência

| Item | Valor |
|---|---|
| Export bronze | `@EXPORT@` (feito em @EXPORTED_AT@, código `@BRONZE_CODE_VERSION@`) |
| Análise (`agm analyze`) | código `@ANALYSIS_CODE_VERSION@` |
| Pacote (`agm package`) | código `@CODE_VERSION@` |
| Amostra | `@SAMPLE@` |

Cada pasta tem um manifesto (`manifest.json`, `analysis_manifest.json`, `classify.json`, ...) com
a versão do código e a configuração usadas. `manifest.json`, na raiz, resume tudo.

As versões de código acima que não são da @TOOL_TAG@ são commits do repositório de
desenvolvimento, anterior à publicação da ferramenta (o histórico publicado foi reescrito para
tirar arquivos de terceiros). A @TOOL_TAG@ reproduz os dois níveis: a partir do export bronze,
`agm analyze` refaz todas as tabelas da análise; a partir delas, os comandos abaixo refazem os
resultados do artigo. Depois do export, só a seção `rq1` do `study.yaml` mudou: ela passou a
registrar o provedor, os modelos e o codebook da coleta final da RQ1. Por isso `agm audit` avisa
que a configuração difere da guardada no export; os números da auditoria não mudam.

## Conteúdo

Cada zip reproduz a estrutura do `DATA_DIR` da ferramenta. Extraídos na mesma pasta, eles formam
um `DATA_DIR` que os comandos leem diretamente.

| Arquivo | Tamanho |
|---|---|
@FILES@

- `dados/bronze-*.zip`: o export do banco (fatos brutos), unidade de replicação;
- `dados/gold-*.zip`: a saída do `agm analyze` (tabelas por questão de pesquisa);
- `rq1/*.zip`: as respostas do LLM em cache, os rótulos e os manifestos da RQ1 (coleta e validação
  externa);
- `resultados/`: auditoria, tabelas da RQ2 e figuras do artigo (LaTeX), em zip e soltos;
- `config/`: o `study.yaml` e os codebooks usados, e as fontes fixadas do *Agent READMEs*;
- `DICIONARIO_DE_DADOS.md`: a descrição de cada tabela e coluna;
- `licencas.csv`: a licença de cada repositório cujo conteúdo foi publicado;
- `SHA256SUMS`: o hash de cada arquivo (`sha256sum -c SHA256SUMS`).

## Como reproduzir

Instale a ferramenta na @TOOL_TAG@ (veja a documentação dela) e rode:

```bash
sha256sum -c SHA256SUMS          # confere os arquivos
./reproduzir.sh L1               # dos resultados publicados (minutos; sem banco e sem chave)
./reproduzir.sh L2               # refaz também o agm analyze a partir do export bronze
```

O script extrai os dados em `./data`, guarda os resultados publicados em `./data/publicado/` e os
refaz com os mesmos comandos do artigo. A RQ1 sai das respostas guardadas
(`agm rq1 classify --offline`): nenhuma requisição é enviada ao LLM, e os pedidos que ficaram
sem resposta na coleta continuam sem resposta. O passo a passo, inclusive como coletar de novo,
está em `docs/replicacao.md` da ferramenta.

| Questão | Entrada | Comando |
|---|---|---|
| RQ1: conteúdo | `rq1_sections` e `rq1/<export>/` | `agm rq1 classify --offline` |
| Validação da RQ1 | `rq1/agentreadmes/` | `agm rq1 agentreadmes score` |
| RQ2: estrutura | `rq2_structure` | `agm rq2 summary` |
| Amostra e vieses | export bronze | `agm audit` |
| Figuras do artigo | gold, auditoria e RQ1 | `agm figures` |

## Dados pessoais

Nomes, e-mails, logins do GitHub e chaves de identidade de pessoas foram trocados por
pseudônimos estáveis (HMAC-SHA256 com uma chave secreta, não publicada): a mesma pessoa tem o
mesmo pseudônimo em todas as tabelas, então as junções continuam valendo, mas o valor original
não pode ser recuperado. Bots e agentes de código mantêm o que os identifica (o nome, como
`dependabot[bot]`, ou o e-mail, como `noreply@anthropic.com`), porque a análise os reconhece por
isso; a outra parte é trocada como a de uma pessoa. Os pares "Nome <e-mail>" e os e-mails dentro
das mensagens de commit também foram trocados. O retrato de progresso das
execuções (`pipeline_runs.progress`), que identifica a máquina da coleta, foi removido. Colunas
pseudonimizadas:

@PERSONAL@

O conteúdo dos arquivos minerados é publicado como está (são arquivos públicos, sob licenças
OSI) e pode conter contatos que os autores puseram neles.

## Licenças

Veja `LICENSE-DATA.md`: CC BY 4.0 para os dados e resultados; o conteúdo minerado segue a licença
de cada repositório (`licencas.csv`). Os arquivos do *Agent READMEs* não estão no pacote, porque
o dataset não declara licença: o `reproduzir.sh` os baixa das revisões fixadas e confere o hash.

## Como citar

Cite o artigo e este pacote pelo DOI do registro no Zenodo, e a ferramenta pela versão usada:
@TOOL@.
"""
