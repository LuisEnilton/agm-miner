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
        "defaultBranchRef": {
            "name": "main",
            "target": {"committedDate": "2026-05-19T00:00:00Z", "history": {"totalCount": 340}},
        },
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
    assert parsed.last_commit_at == datetime(2026, 5, 19, tzinfo=UTC)


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
        ({"createdAt": "2024-01-01T00:00:00Z"}, "created_after_cutoff"),
        ({"createdAt": "2023-12-31T23:59:59Z"}, None),
        ({"licenseInfo": None}, "license"),
        ({"licenseInfo": {"spdxId": "NOASSERTION"}}, "license"),
        ({"licenseInfo": {"spdxId": "CC-BY-4.0"}}, "license"),  # not an OSI license
        ({"licenseInfo": {"spdxId": "apache-2.0"}}, None),  # SPDX ids are case-insensitive
        ({"primaryLanguage": None}, "no_code_language"),
        ({"primaryLanguage": {"name": "HTML"}}, "no_code_language"),
        ({"primaryLanguage": {"name": "Markdown"}}, "no_code_language"),
        ({"primaryLanguage": {"name": "Rust"}}, None),
        ({"stargazerCount": 49}, "min_stars"),
        (
            {"defaultBranchRef": {"name": "main", "target": {"history": {"totalCount": 3}}}},
            "min_commits",
        ),
        # Activity is the latest commit of the default branch, not a push to any branch.
        (
            {
                "pushedAt": "2026-05-30T00:00:00Z",
                "defaultBranchRef": {
                    "name": "main",
                    "target": {
                        "committedDate": "2025-05-01T00:00:00Z",
                        "history": {"totalCount": 340},
                    },
                },
            },
            "inactive",
        ),
        ({"nameWithOwner": "org/awesome-claude-code"}, "keyword:awesome"),
        ({"description": "A tutorial on agents"}, "keyword:tutorial"),
        # Singular and plural forms match the same keyword.
        ({"nameWithOwner": "org/react-starters"}, "keyword:starter"),
        ({"description": "Tutorials for agents"}, "keyword:tutorial"),
        # Ambiguous words are not keywords: they would reject real software.
        ({"description": "A template engine, for example"}, None),
        ({"description": "Collection of useful prompts"}, None),
        # Words ending in "ss" are not plurals.
        ({"description": "Process manager"}, None),
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
    old = {"name": "main", "target": {"committedDate": "2019-01-01T00:00:00Z"}}
    assert evaluate(metadata(defaultBranchRef=old), config, REFERENCE) is None


def test_new_criteria_can_be_disabled() -> None:
    config = SelectionConfig(
        created_before=None, allowed_licenses=None, require_code_language=False
    )
    repository = metadata(
        createdAt="2026-01-01T00:00:00Z", licenseInfo=None, primaryLanguage={"name": "HTML"}
    )
    assert evaluate(repository, config, REFERENCE) is None


class FakeGraphQL:
    def __init__(
        self, nodes: dict[str, dict[str, Any] | None], by_name: dict[str, Any] | None = None
    ):
        self.nodes = nodes
        self.by_name = by_name or {}
        self.calls: list[dict[str, Any]] = []

    def observe_waits(self, observer) -> None:
        pass  # the fake never waits

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


@pytest.mark.db
def test_reused_names_do_not_break_selection(session_factory) -> None:
    """GitHub may give the name of a renamed/deleted repository to another one."""
    with session_factory() as session, session.begin():
        # Row 1 still has the old name "org/app"; repository 2 is the one now called "org/app".
        session.add(Repository(id=1, node_id="R_1", full_name="org/app"))
        session.add(Repository(id=2, node_id="R_2", full_name="org/app-new"))
    fake = FakeGraphQL(
        {"R_1": node(1, nameWithOwner="org/renamed"), "R_2": node(2, nameWithOwner="org/app")}
    )
    study = StudyConfig.model_validate({"selection": {"reference_date": "2026-06-01"}})
    assert select_repositories(study, session_factory, fake)["selected"] == 2
    with session_factory() as session:
        assert session.get(Repository, 1).full_name == "org/renamed"
        assert session.get(Repository, 2).full_name == "org/app"


@pytest.mark.db
def test_add_repository_whose_name_belongs_to_a_stale_row(session_factory) -> None:
    with session_factory() as session, session.begin():
        session.add(Repository(id=5, node_id="R_5", full_name="org/repo9"))
    fake = FakeGraphQL({}, by_name={"org/repo9": node(9)})
    study = StudyConfig.model_validate({"selection": {"reference_date": "2026-06-01"}})
    assert add_repositories(study, session_factory, fake, ["org/repo9"]) == {"added": 1}
    with session_factory() as session:
        names = session.scalars(select(Repository.id).where(Repository.full_name == "org/repo9"))
        assert sorted(names) == [5, 9]


@pytest.mark.db
def test_legacy_node_ids_are_matched_by_position(session_factory) -> None:
    """The REST search API returns legacy ids; GraphQL answers with the new-format id."""
    with session_factory() as session, session.begin():
        session.add(Repository(id=1, node_id="MDEwOlJlcG9zaXRvcnkx", full_name="org/repo1"))

    class Canonicalising(FakeGraphQL):
        def __init__(self) -> None:
            super().__init__({})

        def graphql(self, query, variables):
            assert variables == {"ids": ["MDEwOlJlcG9zaXRvcnkx"]}
            return {"data": {"nodes": [node(1, id="R_kgDOAAAAAQ")]}}

    study = StudyConfig.model_validate({"selection": {"reference_date": "2026-06-01"}})
    assert select_repositories(study, session_factory, Canonicalising()) == {"selected": 1}
    with session_factory() as session:
        assert session.get(Repository, 1).node_id == "R_kgDOAAAAAQ"


def test_fetch_metadata_rejects_misaligned_answers() -> None:
    class Short:
        def graphql(self, query, variables):
            return {"data": {"nodes": [node(1)]}}

    with pytest.raises(SelectionError, match="1 nodes for 2 ids"):
        fetch_metadata(Short(), ["R_1", "R_2"])


@pytest.mark.db
def test_reevaluation_covers_mined_repositories(session_factory) -> None:
    with session_factory() as session, session.begin():
        session.add(
            Repository(id=1, node_id="R_1", full_name="org/repo1", status=RepositoryStatus.MINED)
        )
        session.add(
            Repository(id=2, node_id="R_2", full_name="org/repo2", status=RepositoryStatus.MINED)
        )
        session.add(
            Repository(id=3, node_id="R_3", full_name="org/repo3", status=RepositoryStatus.FAILED)
        )
    fake = FakeGraphQL(
        {"R_1": node(1), "R_2": node(2, nameWithOwner="org/mern-stack-tutorial"), "R_3": node(3)}
    )
    study = StudyConfig.model_validate({"selection": {"reference_date": "2026-06-01"}})
    assert select_repositories(study, session_factory, fake) == {}
    stats = select_repositories(study, session_factory, fake, reevaluate=True)
    assert stats == {"selected": 2, "rejected": 1, "rejected:keyword": 1}
    with session_factory() as session:
        assert session.get(Repository, 1).status is RepositoryStatus.MINED
        assert session.get(Repository, 2).status is RepositoryStatus.REJECTED
        assert session.get(Repository, 3).status is RepositoryStatus.FAILED
