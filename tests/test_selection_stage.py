from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select

from agm_miner.config import SelectionConfig, StudyConfig
from agm_miner.database.models import DiscoveryHit, Repository, RepositoryStatus
from agm_miner.stages.selection import (
    MANUAL_QUERY,
    RepositoryMetadata,
    SelectionError,
    add_repositories,
    evaluate,
    fetch_metadata,
    funnel,
    select_repositories,
)

REFERENCE = date(2026, 6, 1)


def node(repo_id: int, **overrides: Any) -> dict[str, Any]:
    base = {
        "id": f"R_{repo_id}",
        "databaseId": repo_id,
        "nameWithOwner": f"org/repo{repo_id}",
        "createdAt": "2023-01-01T00:00:00Z",
        "pushedAt": "2026-05-20T00:00:00Z",
        "stargazerCount": 120,
        "forkCount": 10,
        "diskUsage": 2048,
        "description": "A real web framework",
        "isFork": False,
        "isArchived": False,
        "isTemplate": False,
        "isMirror": False,
        "isEmpty": False,
        "watchers": {"totalCount": 7},
        "primaryLanguage": {"name": "Python"},
        "licenseInfo": {"spdxId": "MIT"},
        "repositoryTopics": {"nodes": [{"topic": {"name": "web"}}]},
        "defaultBranchRef": {"name": "main", "target": {"history": {"totalCount": 340}}},
    }
    base.update(overrides)
    return base


def metadata(**overrides: Any) -> RepositoryMetadata:
    return RepositoryMetadata.from_graphql(node(1, **overrides))


def test_metadata_parsing() -> None:
    parsed = metadata()
    assert parsed.database_id == 1 and parsed.node_id == "R_1"
    assert parsed.pushed_at == datetime(2026, 5, 20, tzinfo=UTC)
    assert parsed.topics == ("web",)
    assert parsed.commits_count == 340 and parsed.default_branch == "main"
    assert parsed.license_spdx == "MIT"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({}, None),
        ({"isEmpty": True}, "empty"),
        ({"defaultBranchRef": None}, "empty"),
        ({"isFork": True}, "fork"),
        ({"isArchived": True}, "archived"),
        ({"isMirror": True}, "mirror"),
        ({"isTemplate": True}, "template"),
        ({"stargazerCount": 49}, "min_stars"),
        (
            {"defaultBranchRef": {"name": "main", "target": {"history": {"totalCount": 3}}}},
            "min_commits",
        ),
        ({"pushedAt": "2025-05-01T00:00:00Z"}, "inactive"),
        ({"nameWithOwner": "org/awesome-claude-code"}, "keyword:awesome"),
        ({"description": "A tutorial on agents"}, "keyword:tutorial"),
        (
            {"repositoryTopics": {"nodes": [{"topic": {"name": "boilerplate"}}]}},
            "keyword:boilerplate",
        ),
        # Whole words only: "templating" is not "template"; the owner name is ignored.
        ({"description": "Fast templating engine", "nameWithOwner": "awesome/engine"}, None),
        # Order: the first failing criterion wins.
        ({"isFork": True, "stargazerCount": 1}, "fork"),
    ],
)
def test_evaluate(overrides: dict[str, Any], reason: str | None) -> None:
    assert evaluate(metadata(**overrides), SelectionConfig(), REFERENCE) == reason


def test_activity_criterion_can_be_disabled() -> None:
    config = SelectionConfig(active_within_days=None)
    assert evaluate(metadata(pushedAt="2019-01-01T00:00:00Z"), config, REFERENCE) is None


class FakeGraphQL:
    def __init__(
        self, nodes: dict[str, dict[str, Any] | None], by_name: dict[str, Any] | None = None
    ):
        self.nodes = nodes
        self.by_name = by_name or {}
        self.calls: list[dict[str, Any]] = []

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(variables)
        if "ids" in variables:
            found = [self.nodes.get(node_id) for node_id in variables["ids"]]
            errors = [
                {"type": "NOT_FOUND", "path": ["nodes", i]}
                for i, n in enumerate(found)
                if n is None
            ]
            return {"data": {"nodes": found}, **({"errors": errors} if errors else {})}
        key = f"{variables['owner']}/{variables['name']}"
        repository = self.by_name.get(key)
        if repository is None:
            return {"data": {"repository": None}, "errors": [{"type": "NOT_FOUND"}]}
        return {"data": {"repository": repository}}


def test_fetch_metadata_raises_on_unexpected_errors() -> None:
    class Broken:
        def graphql(self, query, variables):
            return {"data": None, "errors": [{"type": "MAX_NODE_LIMIT_EXCEEDED"}]}

    with pytest.raises(SelectionError, match="MAX_NODE_LIMIT_EXCEEDED"):
        fetch_metadata(Broken(), ["R_1"])


@pytest.mark.db
def test_select_repositories_updates_status_and_metadata(session_factory) -> None:
    with session_factory() as session, session.begin():
        for repo_id in (1, 2, 3):
            session.add(
                Repository(id=repo_id, node_id=f"R_{repo_id}", full_name=f"org/old{repo_id}")
            )
    fake = FakeGraphQL({"R_1": node(1), "R_2": node(2, stargazerCount=3), "R_3": None})
    study = StudyConfig.model_validate(
        {"selection": {"reference_date": "2026-06-01", "graphql_batch_size": 2}}
    )
    stats = select_repositories(study, session_factory, fake)

    assert stats == {
        "selected": 1,
        "rejected": 2,
        "rejected:min_stars": 1,
        "rejected:not_found": 1,
    }
    assert [len(call["ids"]) for call in fake.calls] == [2, 1]
    with session_factory() as session:
        first = session.get(Repository, 1)
        assert first.status is RepositoryStatus.SELECTED
        assert first.full_name == "org/repo1"  # renamed repositories get their current name
        assert first.stars == 120 and first.topics == ["web"] and first.commits_count == 340
        assert first.metadata_fetched_at is not None
        assert session.get(Repository, 2).rejection_reason == "min_stars"
        assert session.get(Repository, 3).rejection_reason == "not_found"
        assert sorted(funnel(session), key=str) == sorted(
            [("REJECTED", "min_stars", 1), ("REJECTED", "not_found", 1), ("SELECTED", None, 1)],
            key=str,
        )

    # Only DISCOVERED repositories are processed unless re-evaluation is requested.
    assert select_repositories(study, session_factory, fake) == {}
    relaxed = StudyConfig.model_validate(
        {"selection": {"reference_date": "2026-06-01", "min_stars": 1}}
    )
    assert select_repositories(relaxed, session_factory, fake, reevaluate=True)["selected"] == 2


@pytest.mark.db
def test_add_repositories_registers_and_tags_manual_additions(session_factory) -> None:
    fake = FakeGraphQL({}, by_name={"org/repo9": node(9)})
    study = StudyConfig.model_validate({"selection": {"reference_date": "2026-06-01"}})
    assert add_repositories(study, session_factory, fake, ["org/repo9"]) == {"added": 1}
    assert add_repositories(study, session_factory, fake, ["org/repo9"]) == {"added": 1}
    with session_factory() as session:
        repository = session.get(Repository, 9)
        assert repository.status is RepositoryStatus.SELECTED and repository.node_id == "R_9"
        hit = session.scalars(select(DiscoveryHit)).one()
        assert hit.query == MANUAL_QUERY
    with pytest.raises(SelectionError, match="not found"):
        add_repositories(study, session_factory, fake, ["org/missing"])
