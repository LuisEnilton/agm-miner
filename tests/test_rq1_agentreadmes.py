"""External validation on the Agent READMEs dataset: import, label mapping and score."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from agm_miner.cli import app
from agm_miner.config import Rq1Config
from agm_miner.rq1.agentreadmes import (
    GPT5,
    HUMAN,
    AgentReadmesError,
    OutputExistsError,
    build_files,
    build_sections,
    import_agentreadmes,
    labelled_artifact_ids,
    parse_gpt5_labels,
    parse_human_labels,
    score_agentreadmes,
)
from agm_miner.rq1.backends import PromptRequest
from agm_miner.rq1.codebook import load_codebook
from agm_miner.rq1.llm import LLMResult
from agm_miner.rq1.stages import classify_sections
from tests.test_rq1_labels import FakeBackend

SEED = Path(__file__).resolve().parents[1] / "codebooks" / "seed_agent_readmes.yaml"
URL = "https://github.com/{}/blob/main/{}"

FILES = [  # (owner/name, agent, path, content)
    ("a/one", "claude", "CLAUDE.md", "# Proj\n\n## Testing\nRun pytest.\n\n## Build\nmake\n"),
    ("b/two", "agents", "AGENTS.md", "## Build\nnpm run build\n"),
    (
        "c/three",
        "copilot-instructions",
        ".github/copilot-instructions.md",
        "## Security\nNever commit secrets.\n",
    ),
    ("d/four", "agents", "docs/AGENTS.md", "# Only a title\n"),
    ("e/five", "claude", "CLAUDE.md", "## Testing\nRun the suite.\n"),
]
HUMAN_ROWS = [  # (#, file, labels)
    ("1", "a/one", ["Test", "Build & Run"]),
    ("2", "b/two", ["Build & Run", "Documentation"]),
    ("3", "c/three", ["Security"]),
    ("4", "d/four", ["System Overview"]),
    ("5", "z/missing", ["Test"]),  # labelled, but not in the dataset
    ("6", "e/five", []),  # a row without labels stays in, with no category
]
GPT5_ROWS = {
    "1": "['test', 'build_run']",
    "2": "['build_run']",
    "3": "['security', 'ai_integration']",
    "4": "[]",
    "5": "['test']",
    "6": "['test']",
}


def url_of(name: str) -> str:
    path = next((f[2] for f in FILES if f[0] == name), "CLAUDE.md")
    return URL.format(name, path)


def dataset() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "content": content,
                "agent": agent,
                "repository_owner": name.split("/")[0],
                "repository_name": name.split("/")[1],
                "file_path": path,
                "filename": path.rsplit("/", 1)[-1],
                "file_url": url_of(name),
                "stars": 100.0,
                "created_at": "2025-01-01T00:00:00Z",
                "content_commit_sha": "abc",
            }
            for name, agent, path, content in FILES
        ]
    )


def write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    data = tmp_path / "train.parquet"
    dataset().write_parquet(data)
    human = tmp_path / "final_am_classification_3.csv"
    with human.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["#", "repository_owner", "file_url", "Label1", "Label2", "Label3"])
        for row_id, name, labels in HUMAN_ROWS:
            writer.writerow([row_id, name.split("/")[0], url_of(name), *labels, *[""] * 3][:6])
    gpt5 = tmp_path / "gpt_5_file_classifications.csv"
    with gpt5.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ID", "reasoning", "type", "confidence"])
        for row_id, types in GPT5_ROWS.items():
            writer.writerow([row_id, "because", types, 8])
    return data, human, gpt5


# --------------------------------------------------------------------------- import


def test_files_get_content_derived_ids_and_tools() -> None:
    files = build_files(dataset())
    assert files["artifact_id"].to_list() == [1, 2, 3, 4, 5]  # sorted by URL
    assert files["file_url"].is_sorted()
    assert dict(zip(files["full_name"], files["repo_id"], strict=True)) == {
        "a/one": 1,
        "b/two": 2,
        "c/three": 3,
        "d/four": 4,
        "e/five": 5,
    }
    assert set(files["tool"]) == {"CLAUDE", "AGENTS", "COPILOT"}
    # Same input in another order, same ids.
    shuffled = build_files(dataset().reverse())
    assert shuffled.select("file_url", "artifact_id").equals(
        files.select("file_url", "artifact_id")
    )

    with pytest.raises(AgentReadmesError, match="unknown agent"):
        build_files(dataset().with_columns(pl.lit("cursor").alias("agent")))
    with pytest.raises(AgentReadmesError, match="duplicated file_url"):
        build_files(pl.concat([dataset(), dataset().head(1)]))
    with pytest.raises(AgentReadmesError, match="no column"):
        build_files(dataset().drop("content"))


def test_sections_are_parsed_like_the_analysis() -> None:
    sections = build_sections(build_files(dataset()))
    first = sections.filter(pl.col("full_name") == "a/one")
    assert first["heading_path"].to_list() == [["Proj"], ["Proj", "Testing"], ["Proj", "Build"]]
    assert first["text"].to_list() == ["", "Run pytest.", "make"]
    assert first["h2"].to_list() == [None, "Testing", "Build"]
    scope = dict(zip(sections["path"], sections["scope"], strict=True))
    assert (
        scope["docs/AGENTS.md"] == "NESTED" and scope[".github/copilot-instructions.md"] == "ROOT"
    )
    assert set(sections["kind"]) == {"INSTRUCTION"}


def test_reference_labels_map_onto_the_seed_codebook() -> None:
    seed = set(load_codebook(SEED).ids)
    frame = pl.DataFrame(
        {
            "#": ["1", "2"],
            "file_url": ["u1", "u2"],
            "Label1": ["Maintanability", None],
            "Label2": ["Claude/AI Integration", ""],
        }
    )
    assert parse_human_labels(frame) == {
        "1": ("u1", ["ai_integration", "maintenance"]),
        "2": ("u2", []),
    }
    # The published CSV has a URL with a leading space: it must still match the dataset.
    spaced = frame.with_columns(pl.Series("file_url", [" u1", "u2 "]))
    assert [url for url, _ in parse_human_labels(spaced).values()] == ["u1", "u2"]
    with pytest.raises(AgentReadmesError, match="unknown human labels"):
        parse_human_labels(frame.with_columns(pl.lit("Vibes").alias("Label1")))

    gpt5 = parse_gpt5_labels(
        pl.DataFrame({"ID": ["1", "2"], "type": ['["test", "build_run"]', "['maintainability']"]})
    )
    assert gpt5 == {"1": ["build_and_run", "testing"], "2": ["maintenance"]}
    with pytest.raises(AgentReadmesError, match="unknown GPT-5 labels"):
        parse_gpt5_labels(pl.DataFrame({"ID": ["1"], "type": ["['vibes']"]}))

    from agm_miner.rq1.agentreadmes import GPT5_LABELS, HUMAN_LABELS

    assert set(HUMAN_LABELS.values()) == seed and set(GPT5_LABELS.values()) == seed


def test_import_writes_an_analysis_like_output(tmp_path: Path) -> None:
    data, human, gpt5 = write_inputs(tmp_path)
    output = tmp_path / "external" / "agentreadmes"
    summary = import_agentreadmes(data, output, human_path=human, gpt5_path=gpt5)

    assert summary["files"] == 5 and summary["files_by_tool"] == {
        "AGENTS": 2,
        "CLAUDE": 2,
        "COPILOT": 1,
    }
    assert summary["human_files"] == 6 and summary["human_files_in_dataset"] == 5
    assert summary["human_files_without_label"] == 1 and summary["gpt5_files"] == 6
    assert summary["missing_from_dataset"] == [url_of("z/missing")]
    assert summary["sections_with_text"] == 5  # the two title-only sections have no text

    manifest = json.loads((output / "analysis_manifest.json").read_text())
    assert set(manifest["inputs"]) == {"dataset", "human_labels", "gpt5_labels"}
    assert len(manifest["inputs"]["dataset"]["sha256"]) == 64
    reference = pl.read_parquet(output / "reference_labels.parquet")
    assert set(reference["rater"]) == {HUMAN, GPT5} and reference.height == 12
    files = pl.read_parquet(output / "files.parquet")
    assert files.filter(pl.col("full_name") == "d/four")["sections"].item() == 0
    assert not (tmp_path / "external" / "agentreadmes.partial").exists()

    with pytest.raises(OutputExistsError):
        import_agentreadmes(data, output)
    assert import_agentreadmes(data, output, force=True)["files"] == 5
    assert not (output / "reference_labels.parquet").exists()  # replaced without labels
    with pytest.raises(AgentReadmesError, match="row ids"):
        import_agentreadmes(data, tmp_path / "other", gpt5_path=gpt5)


# --------------------------------------------------------------------------- score


class HeadingBackend(FakeBackend):
    """Classifies a section by its last heading (Testing, Build, Security), else ``other``."""

    CATEGORIES = {"Testing": "testing", "Build": "build_and_run", "Security": "security"}
    _SECTION = re.compile(r'<section id="(s\d+)">\n[^\n]*\n[^\n]*\nheading path: ([^\n]*)')

    def answer(self, request: PromptRequest) -> LLMResult:
        self.asked.append(request.custom_id)
        answers = [
            {"id": short_id, "categories": [self.CATEGORIES.get(path.split(" > ")[-1], "other")]}
            for short_id, path in self._SECTION.findall(request.user)
        ]
        text = json.dumps({"sections": answers})
        return LLMResult(request.custom_id, "succeeded", text=text, usage={"input_tokens": 1})


def imported(tmp_path: Path) -> tuple[Path, Path, Rq1Config]:
    data, human, gpt5 = write_inputs(tmp_path)
    gold = tmp_path / "external" / "agentreadmes"
    import_agentreadmes(data, gold, human_path=human, gpt5_path=gpt5)
    config = Rq1Config(provider="openrouter", classify_model="fake-model")
    return gold, tmp_path / "rq1" / gold.name, config


def test_score_compares_files_with_the_human_labels_and_gpt5(tmp_path: Path) -> None:
    gold, output, config = imported(tmp_path)
    classify_sections(gold, output, config, HeadingBackend(), codebook_path=SEED)
    scores = score_agentreadmes(
        gold, output, config, codebook_path=SEED, provider="openrouter", model="fake-model"
    )

    # Files 1-4 and 6 are labelled and in the dataset; file 4 has no text (no category).
    assert scores["files"] == 5 and scores["files_without_text"] == 1
    assert scores["unclassified_files"] == 0
    ours = scores["human_vs_llm"]
    # Human: 2 + 2 + 1 + 1 + 0 = 6 labels; ours: 2 + 1 + 1 + 0 + 1 (file 6: testing) = 5;
    # agreeing: 4.
    assert ours["files"] == 5 and ours["micro_f1"] == pytest.approx(
        2 * 0.8 * (4 / 6) / (0.8 + 4 / 6)
    )
    baseline = scores["human_vs_gpt5"]
    assert baseline["files"] == 5 and baseline["macro_f1"] is not None
    assert set(scores["by_tool"]) == {"AGENTS", "CLAUDE", "COPILOT"}
    assert scores["by_tool"]["COPILOT"]["human_vs_llm"]["micro_f1"] == 1.0

    directory = Path(scores["output"]).parent
    report = (directory / "scores.md").read_text()
    assert "## Human × GPT-5 (paper's baseline)" in report and "| COPILOT |" in report
    manifest = json.loads((directory / "scores.manifest.json").read_text())
    assert manifest["files"] == 5 and manifest["analysis_manifest"]["files"] == 5

    only_ours = score_agentreadmes(
        gold,
        output,
        config,
        codebook_path=SEED,
        provider="openrouter",
        model="fake-model",
        tools=["CLAUDE", "AGENTS"],
    )
    assert only_ours["files"] == 4 and only_ours["tools"] == ["AGENTS", "CLAUDE"]
    assert set(only_ours["by_tool"]) == {"AGENTS", "CLAUDE"}
    assert Path(only_ours["output"]).name == "scores-AGENTS-CLAUDE.json"
    assert (directory / "scores.json").exists()  # the unfiltered score is kept

    with pytest.raises(AgentReadmesError, match="unknown tools"):
        score_agentreadmes(
            gold,
            output,
            config,
            codebook_path=SEED,
            provider="openrouter",
            model="fake-model",
            tools=["CURSOR"],
        )


def test_score_leaves_out_files_whose_classification_is_incomplete(tmp_path: Path) -> None:
    gold, output, config = imported(tmp_path)
    classify_sections(gold, output, config, HeadingBackend(), codebook_path=SEED, limit=1)
    scores = score_agentreadmes(
        gold, output, config, codebook_path=SEED, provider="openrouter", model="fake-model"
    )
    # Only a/one was classified; d/four has no text, so it is scored as "no category".
    assert scores["files"] == 2 and scores["unclassified_files"] == 3


def test_score_needs_the_seed_codebook_and_a_classification(tmp_path: Path) -> None:
    gold, output, config = imported(tmp_path)
    with pytest.raises(FileNotFoundError, match="agm rq1 classify"):
        score_agentreadmes(
            gold, output, config, codebook_path=SEED, provider="openrouter", model="fake-model"
        )
    other = tmp_path / "rq1.yaml"
    other.write_text(
        "name: rq1\nversion: t\ncategories:\n- {id: testing, name: T, definition: d}\n"
    )
    with pytest.raises(AgentReadmesError, match="seed_agent_readmes"):
        score_agentreadmes(
            gold, output, config, codebook_path=other, provider="openrouter", model="fake-model"
        )


def test_agentreadmes_commands(tmp_path: Path, monkeypatch) -> None:
    data, human, gpt5 = write_inputs(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "rq1",
            "agentreadmes",
            "import",
            str(data),
            "--human-labels",
            str(human),
            "--gpt5-labels",
            str(gpt5),
        ],
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output[result.output.index("{") :])
    assert summary["output"] == str(tmp_path / "data" / "external" / "agentreadmes")

    again = runner.invoke(app, ["rq1", "agentreadmes", "import", str(data)])
    assert again.exit_code == 1 and "--force" in again.output

    unscored = runner.invoke(app, ["rq1", "agentreadmes", "score", "--codebook", str(SEED)])
    assert unscored.exit_code == 1 and "agm rq1 classify" in unscored.output

    # Classified with another model than study.yaml's: --model points the score at it.
    gold = tmp_path / "data" / "external" / "agentreadmes"
    config = Rq1Config(provider="openrouter", classify_model="fake-model")
    output = tmp_path / "data" / "rq1" / "agentreadmes"
    classify_sections(gold, output, config, HeadingBackend(), codebook_path=SEED)
    scored = runner.invoke(
        app,
        [
            "rq1",
            "agentreadmes",
            "score",
            "--codebook",
            str(SEED),
            "--model",
            "openrouter:fake-model",
        ],
    )
    assert scored.exit_code == 0, scored.output
    assert json.loads(scored.output[scored.output.index("{") :])["files"] == 5


def test_classify_only_the_labelled_files_then_everything_from_the_cache(tmp_path: Path) -> None:
    gold, output, config = imported(tmp_path)
    labelled = labelled_artifact_ids(gold)
    assert len(labelled) == 5  # z/missing is labelled but not in the dataset

    first = HeadingBackend()
    only_one = classify_sections(
        gold, output, config, first, codebook_path=SEED, artifact_ids=labelled[:1]
    )
    assert only_one["files"] == 1 and only_one["only_files"] == 1
    assert len(first.asked) == 1

    everything = HeadingBackend()
    summary = classify_sections(gold, output, config, everything, codebook_path=SEED)
    assert summary["only_files"] is None and summary["files"] == 4  # d/four has no text
    assert len(everything.asked) == summary["requests_planned"] - 1  # file 1 from the cache

    without_labels = tmp_path / "plain"
    import_agentreadmes(tmp_path / "train.parquet", without_labels)
    with pytest.raises(FileNotFoundError, match="--human-labels"):
        labelled_artifact_ids(without_labels)


def test_enriched_codebook_keeps_the_seed_categories() -> None:
    seed = load_codebook(SEED)
    enriched = load_codebook(SEED.with_name("agent_readmes_enriched.yaml"))
    assert enriched.label != seed.label  # its classification lands in another folder
    as_seed = [(c.id, c.name, c.group, c.definition) for c in seed.categories]
    assert [(c.id, c.name, c.group, c.definition) for c in enriched.categories] == as_seed
    assert all(c.include.startswith("In the paper:") for c in enriched.categories)
    assert all("Operational definition:" in c.include for c in enriched.categories)
