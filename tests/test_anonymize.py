"""Pseudonymisation of the personal data of the replication package (no database)."""

from __future__ import annotations

import polars as pl
import pytest

from agm_miner.config import AnalysisConfig
from agm_miner.replication.anonymize import (
    PERSONAL_COLUMNS,
    Pseudonymizer,
    anonymize_frame,
    has_personal_data,
)

SALT = b"0123456789abcdef-test-salt"
P = Pseudonymizer(SALT, AnalysisConfig())

ANA = ("Ana Dev", "ana@example.org")
BOT = ("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com")
CLAUDE = ("Claude", "noreply@anthropic.com")


def test_pseudonyms_are_stable_salted_and_typed() -> None:
    assert P.email("ana@example.org") == P.email("ana@example.org")
    assert P.email("ana@example.org") != P.email("bia@example.org")
    other = Pseudonymizer(b"another-salt-of-16+bytes", AnalysisConfig())
    assert other.email("ana@example.org") != P.email("ana@example.org")
    assert P.name("Ana Dev").startswith("person-") and "Ana" not in P.name("Ana Dev")
    assert P.email("ana@example.org").endswith("@users.invalid")
    assert P.login("ana").startswith("user-") and P.identity_key("x").startswith("id-")
    assert P.name(None) is None and P.email("") == ""
    with pytest.raises(ValueError, match="16 bytes"):
        Pseudonymizer(b"short", AnalysisConfig())


def test_bots_and_agents_keep_what_identifies_them() -> None:
    assert P.person(*BOT) == BOT
    # Claude is recognised by its e-mail: the e-mail stays, the name is a person's name.
    assert P.person(*CLAUDE) == (P.name("Claude"), CLAUDE[1])
    assert P.person("Cedric van Putten", CLAUDE[1]) == (P.name("Cedric van Putten"), CLAUDE[1])
    assert P.automated(*P.person(*CLAUDE))
    name, email = P.person(*ANA)
    assert name != ANA[0] and email != ANA[1]
    # A pseudonym never looks like a bot or an agent to the analysis.
    assert not P.automated(name, email)


def test_emails_in_messages_follow_the_email_columns() -> None:
    message = (
        "Fix build\n\nSigned-off-by: Ana Dev <ana@example.org>\n"
        "Co-authored-by: Claude <noreply@anthropic.com>\n"
        "Co-authored-by: dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>"
    )
    text = P.text(message)
    assert "ana@example.org" not in text and "Ana Dev" not in text
    assert f"Signed-off-by: {P.name('Ana Dev')} <{P.email('ana@example.org')}>" in text
    assert P.text("write to ana@example.org") == f"write to {P.email('ana@example.org')}"
    assert "noreply@anthropic.com" in text and "dependabot[bot]@users.noreply.github.com" in text
    assert P.text(None) is None and P.text("no address here") == "no address here"


def test_commits_identities_and_coauthors() -> None:
    commits = pl.DataFrame(
        {
            "sha": ["a", "b"],
            "author_name": [ANA[0], BOT[0]],
            "author_email": [ANA[1], BOT[1]],
            "committer_name": [ANA[0], "GitHub"],
            "committer_email": [ANA[1], "noreply@github.com"],
            "message": ["Signed-off-by: Ana <ana@example.org>", "Bump"],
        }
    )
    out = anonymize_frame("commits", commits, P)
    assert out.schema == commits.schema and out["sha"].to_list() == ["a", "b"]
    assert out["author_email"].to_list() == [P.email(ANA[1]), BOT[1]]
    assert out["committer_name"][1] == P.name("GitHub")  # GitHub's committer is not a bot
    assert out["message"][0] == f"Signed-off-by: {P.name('Ana')} <{P.email(ANA[1])}>"

    identities = pl.DataFrame(
        {
            "id": [1, 2],
            "identity_key": [ANA[1], CLAUDE[1]],
            "name": [ANA[0], CLAUDE[0]],
            "email": [ANA[1], CLAUDE[1]],
            "github_login": ["ana", None],
        }
    )
    out = anonymize_frame("identities", identities, P)
    assert out.row(0) == (
        1,
        P.identity_key(ANA[1]),
        P.name(ANA[0]),
        P.email(ANA[1]),
        P.login("ana"),
    )
    assert out.row(1) == (2, CLAUDE[1], P.name("Claude"), CLAUDE[1], None)

    coauthors = pl.DataFrame({"name": [CLAUDE[0], ANA[0]], "email": [CLAUDE[1], ANA[1]]})
    out = anonymize_frame("commit_coauthors", coauthors, P)
    assert out.row(0) == (P.name("Claude"), CLAUDE[1])
    assert out.row(1) == (P.name(ANA[0]), P.email(ANA[1]))


def test_gold_tables_and_runs() -> None:
    roles = pl.DataFrame(
        {"name": [ANA[0], BOT[0]], "github_login": ["ana", None], "role": ["mixed", "bot"]}
    )
    out = anonymize_frame("rq3_author_roles", roles, P)
    assert out.row(0) == (P.name(ANA[0]), P.login("ana"), "mixed") and out.row(1) == roles.row(1)

    changes = pl.DataFrame(
        {"message_subject": ["ping ana@example.org"], "message": ["ping ana@example.org\n\nmore"]}
    )
    out = anonymize_frame("rq3_changes", changes, P)
    assert out["message"][0].splitlines()[0] == out["message_subject"][0]

    runs = pl.DataFrame({"id": [1], "progress": ['{"pid": "43079@meu-pc"}']})
    assert anonymize_frame("pipeline_runs", runs, P)["progress"].to_list() == [None]


def test_tables_without_personal_data_are_untouched() -> None:
    frame = pl.DataFrame({"repo_id": [1], "path": ["CLAUDE.md"]})
    assert not has_personal_data("artifacts")
    assert anonymize_frame("artifacts", frame, P) is frame
    assert set(PERSONAL_COLUMNS) == {
        "commits",
        "identities",
        "commit_coauthors",
        "pipeline_runs",
        "rq3_author_roles",
        "rq3_changes",
    }


def test_a_person_under_a_bots_name_keeps_the_name_but_not_the_email() -> None:
    # Found in the final collection: `github-actions <someone@gmail.com>`.
    name, email = P.person("github-actions", "carla@example.net")
    assert name == "github-actions" and email == P.email("carla@example.net")
    assert P.automated(name, email)  # the analysis still sees a bot
    assert P.person(*BOT) == BOT  # a bot's own address stays

    identities = pl.DataFrame(
        {
            "identity_key": ["carla@example.net", BOT[1].lower()],
            "name": ["github-actions", BOT[0]],
            "email": ["carla@example.net", BOT[1]],
            "github_login": ["carla", "dependabot[bot]"],
        }
    )
    out = anonymize_frame("identities", identities, P)
    assert out.row(0) == (
        P.identity_key("carla@example.net"),
        "github-actions",
        P.email("carla@example.net"),
        P.login("carla"),
    )
    assert out.row(1) == identities.row(1)
    roles = pl.DataFrame({"name": ["github-actions"], "github_login": ["carla"], "role": ["bot"]})
    assert anonymize_frame("rq3_author_roles", roles, P).row(0) == (
        "github-actions",
        P.login("carla"),
        "bot",
    )


@pytest.mark.parametrize(
    "text",
    [
        "Bump [@ai-sdk/provider@3.0.14](https://github.com/vercel/ai/blob/@ai-sdk/provider@3.0.14/x)",
        "git remote add origin git@github.com:acme/app.git",
        "see https://example.com/users/ana@example.org/profile",
        "npm i @types/ws@8.5.10",
    ],
)
def test_things_that_are_not_email_addresses_stay(text: str) -> None:
    assert P.text(text) == text


def test_an_address_at_the_end_of_a_sentence_is_replaced() -> None:
    assert P.text("Ask ana@example.org.") == f"Ask {P.email('ana@example.org')}."
    trailer = "Co-authored-by: github-actions <carla@example.net>"
    assert P.text(trailer) == f"Co-authored-by: github-actions <{P.email('carla@example.net')}>"
