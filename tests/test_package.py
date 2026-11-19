"""The replication package: mine → export → analyze → package, then rebuild from the package."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import polars as pl
import pytest

from agm_miner.analysis.datasets import build_datasets
from agm_miner.config import AnalysisConfig, Settings, StudyConfig
from agm_miner.database.models import DiscoveryHit, PipelineRun, Repository, RepositoryStatus
from agm_miner.replication.anonymize import Pseudonymizer
from agm_miner.replication.package import PackageInputError, PackageInputs, build_package
from agm_miner.stages.export import export_bronze
from agm_miner.stages.mining import MiningOptions, mine_repositories
from agm_miner.stages.selection import MANUAL_QUERY
from tests.gitrepo import RepoBuilder

pytestmark = [pytest.mark.db, pytest.mark.git]

ROOT = Path(__file__).resolve().parents[1]
CONFIG = AnalysisConfig(min_artifact_bytes=0, min_human_contributors=1)
SALT = b"a-test-salt-of-more-than-16-bytes"

ANA = ("Ana Dev", "ana@example.org")
BIA = ("Bia Souza", "12345+biasouza@users.noreply.github.com")
BOT = ("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com")
CLAUDE = ("Claude", "noreply@anthropic.com")
PEOPLE = ("Ana Dev", "ana@example.org", "Bia Souza", "biasouza")

V1 = "# Project\n\n## Setup\nInstall with make. Ask ana@example.org.\n"
V2 = V1 + "\n## Testing\nRun pytest before pushing.\n"


@pytest.fixture
def data_dir(tmp_path: Path, session_factory, repo_builder: RepoBuilder) -> Path:
    """DATA_DIR with a bronze export, its analysis and RQ1/result folders, as after a study."""
    repo = repo_builder
    repo.commit("Add app", {"src/app.py": "x = 1\n"}, author=ANA, date="2024-03-01T09:00:00Z")
    repo.commit(
        "Add CLAUDE.md\n\nSigned-off-by: Ana Dev <ana@example.org>\n"
        "Co-authored-by: Claude <noreply@anthropic.com>",
        {"CLAUDE.md": V1},
        author=ANA,
        date="2024-03-02T09:00:00Z",
    )
    repo.commit("Document testing", {"CLAUDE.md": V2}, author=BIA, date="2024-03-03T09:00:00Z")
    repo.commit("Bump", {"pyproject.toml": "[x]\n"}, author=BOT, date="2024-03-10T00:00:00Z")
    repo.commit("Refactor", {"src/app.py": "x = 2\n"}, author=CLAUDE, date="2024-03-20T00:00:00Z")
    with session_factory() as session, session.begin():
        session.add(
            Repository(
                id=7,
                node_id="R_7",
                full_name="acme/app",
                status=RepositoryStatus.SELECTED,
                license_spdx="MIT",
            )
        )
        session.flush()
        run = PipelineRun(
            stage="SELECT",
            status="SUCCEEDED",
            code_version="x",
            study_config_hash="h",
            study_config={},
            progress={"pid": "4242@collection-host"},
        )
        session.add(run)
        session.flush()
        session.add(DiscoveryHit(repo_id=7, query=MANUAL_QUERY, path="", run_id=run.id))
    settings = Settings(CLONE_DIR=str(tmp_path / "clones"))
    stats = mine_repositories(
        settings, StudyConfig(), session_factory, MiningOptions(), clone_url=lambda _: repo.url
    )
    assert stats["mined"] == 1

    data = tmp_path / "data"
    bronze = export_bronze(StudyConfig(), session_factory, data / "bronze")
    build_datasets(bronze, data / "gold" / bronze.name, CONFIG)
    rq1 = data / "rq1" / bronze.name
    (rq1 / "cache" / "ab").mkdir(parents=True)
    (rq1 / "cache" / "ab" / ("ab" + "0" * 62 + ".json")).write_text('{"text": "{}"}')
    (rq1 / "classify").mkdir()
    (rq1 / "classify" / "classify.json").write_text("{}")
    (rq1 / "failures.json").write_text("{}")
    (rq1 / "batches.json").write_text('{"batch_1": ["x"]}')  # pending: must not be published
    (rq1 / "batches.json.lock").write_text("")
    for kind, name in (("audit", "audit.tex"), ("figures", "figuras.tex")):
        (data / kind / bronze.name).mkdir(parents=True)
        (data / kind / bronze.name / name).write_text(f"% {kind}\n")
    (data / "rq2" / f"{bronze.name}__vs__agentreadmes").mkdir(parents=True)
    (data / "rq2" / f"{bronze.name}__vs__agentreadmes" / "rq2_summary.md").write_text("# RQ2\n")
    external = data / "external" / "agentreadmes"
    external.mkdir(parents=True)
    inputs = {"dataset": {"sha256": "d" * 64}, "human_labels": {"sha256": "h" * 64}}
    (external / "analysis_manifest.json").write_text(json.dumps({"inputs": inputs}))
    (external / "file_contents.parquet").write_text("third-party content")
    return data


def _inputs(data: Path, output: Path) -> PackageInputs:
    bronze = next((data / "bronze").iterdir())
    return PackageInputs(
        bronze_dir=bronze,
        gold_dir=data / "gold" / bronze.name,
        data_dir=data,
        study_path=ROOT / "study.yaml",
        codebooks_dir=ROOT / "codebooks",
        output_dir=output,
    )


def _extract(package: Path, destination: Path) -> Path:
    for archive in sorted(package.glob("*/*.zip")):
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(destination)
    return destination


def _strings(directory: Path) -> str:
    """Every string value of every Parquet table under ``directory``, as one text."""
    parts = []
    for path in sorted(directory.rglob("*.parquet")):
        frame = pl.read_parquet(path)
        for column, dtype in frame.schema.items():
            if dtype == pl.String:
                parts += [value for value in frame[column].to_list() if value]
            elif dtype == pl.List(pl.String):
                parts += [v for values in frame[column].to_list() if values for v in values]
    return "\n".join(parts)


def test_package_layout_checksums_and_excluded_files(data_dir: Path, tmp_path: Path) -> None:
    output = tmp_path / "package"
    p = Pseudonymizer(SALT, CONFIG)
    summary = build_package(_inputs(data_dir, output), p, code_version="abc")
    assert summary["output"] == str(output) and not output.with_name("package.partial").exists()
    export = next((data_dir / "bronze").iterdir()).name
    for name in (
        "README.md",
        "LICENSE-DATA.md",
        "DICIONARIO_DE_DADOS.md",
        "manifest.json",
        "SHA256SUMS",
        "reproduzir.sh",
        "licencas.csv",
        "config/study.yaml",
        "config/codebooks/seed_agent_readmes.yaml",
        "config/fontes_agentreadmes.tsv",
        f"dados/bronze-{export}.zip",
        f"dados/gold-{export}.zip",
        f"rq1/rq1-{export}.zip",
        f"resultados/resultados-{export}.zip",
        "resultados/audit.tex",
        "resultados/figuras.tex",
        "resultados/rq2_summary.md",
    ):
        assert (output / name).is_file(), name
    assert len(list(output.rglob("*"))) < 100  # Zenodo's limit of files per record

    for line in (output / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest

    with zipfile.ZipFile(output / "rq1" / f"rq1-{export}.zip") as zipped:
        names = set(zipped.namelist())
    assert (
        f"rq1/{export}/failures.json" in names and f"rq1/{export}/classify/classify.json" in names
    )
    assert not any(name.endswith(("batches.json", ".lock")) for name in names)
    with zipfile.ZipFile(output / f"resultados/resultados-{export}.zip") as zipped:
        assert f"rq2/{export}__vs__agentreadmes/rq2_summary.md" in zipped.namelist()
    everything = "\n".join(str(path) for path in output.rglob("*"))
    assert "file_contents" not in everything  # the Agent READMEs files are not redistributed

    sources = (output / "config" / "fontes_agentreadmes.tsv").read_text()
    assert "d" * 64 in sources and "huggingface.co" in sources
    with (output / "licencas.csv").open() as handle:
        assert list(csv.DictReader(handle)) == [
            {
                "repo_id": "7",
                "full_name": "acme/app",
                "license_spdx": "MIT",
                "url": "https://github.com/acme/app",
            }
        ]
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["code_version"] == "abc" and manifest["export"] == export
    assert "commits" in manifest["pseudonymised"]
    readme = (output / "README.md").read_text()
    assert export in readme and f"dados/bronze-{export}.zip" in readme
    script = output / "reproduzir.sh"
    assert script.stat().st_mode & 0o111 and f"EXPORT={export}" in script.read_text()
    assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0

    with pytest.raises(PackageInputError, match="already exists"):
        build_package(_inputs(data_dir, output), p, code_version="abc")


def test_package_has_no_personal_data_and_rebuilds_the_same_analysis(
    data_dir: Path, tmp_path: Path
) -> None:
    p = Pseudonymizer(SALT, CONFIG)
    output = tmp_path / "package"
    build_package(_inputs(data_dir, output), p, code_version="abc")
    extracted = _extract(output, tmp_path / "extracted")
    export = next((data_dir / "bronze").iterdir()).name
    bronze, gold = extracted / "bronze" / export, extracted / "gold" / export

    text = _strings(bronze) + _strings(gold)
    # People are gone, except inside the file content (public, under the repository licence).
    without_content = text.replace(V1, "").replace(V2, "").replace("Ask ana@example.org.", "")
    for value in PEOPLE:
        assert value not in without_content, value
    assert "4242@collection-host" not in text
    for kept in (BOT[0], CLAUDE[1], CLAUDE[0]):  # bots and agents keep their names
        assert kept in text
    assert p.email(ANA[1]) in text and p.name(BIA[0]) in text

    # `agm analyze` on the pseudonymised bronze gives the pseudonymised gold, table by table.
    rebuilt = tmp_path / "rebuilt"
    build_datasets(bronze, rebuilt, CONFIG)
    for path in sorted(gold.glob("*.parquet")):
        assert pl.read_parquet(rebuilt / path.name).equals(pl.read_parquet(path)), path.name
    roles = pl.read_parquet(gold / "rq3_author_roles.parquet")
    assert set(roles["role"]) >= {"agent", "bot"}
    assert set(roles.filter(pl.col("role") == "bot")["name"]) == {BOT[0]}


def test_package_command_needs_a_salt(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agm_miner.cli import app
    from agm_miner.config import get_settings

    monkeypatch.delenv("PACKAGE_SALT", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env with a salt here
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["package", "--study", str(ROOT / "study.yaml")])
    get_settings.cache_clear()
    assert result.exit_code == 1 and "PACKAGE_SALT is not set" in result.output
