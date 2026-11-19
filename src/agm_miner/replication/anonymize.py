"""Pseudonymisation of the personal data in the published tables (pure functions).

Names, e-mails, GitHub logins and identity keys of people become stable pseudonyms: an HMAC of
the original value with a secret salt (``PACKAGE_SALT``, never published), so the same person
gets the same pseudonym in every table and the joins still work. Bots and coding agents (the
``analysis`` patterns of ``study.yaml``) keep their names: they are not people, and the analysis
recognises them by name, so ``agm analyze`` on the pseudonymised bronze export reproduces the
pseudonymised gold tables, author roles included. E-mails inside commit messages get the same
pseudonym as in the e-mail columns.

The ``progress`` snapshot of ``pipeline_runs`` is dropped: it names the collection machine.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass

import polars as pl

from agm_miner.analysis.authors import detect_agent, is_bot
from agm_miner.config import AnalysisConfig

# An address inside free text (commit messages): local part, @, domain with a dot.
_EMAIL = r"[\w.%+\-\[\]]+@[\w\-]+(?:\.[\w\-]+)+"
# "Name <address>" (trailers such as Signed-off-by, Co-authored-by) or a bare address, matched in
# one pass so a pseudonym is never pseudonymised again.
PERSON_IN_TEXT = re.compile(
    rf"(?P<name>[^\s<>:][^<>:\n]*?)\s*<(?P<email>{_EMAIL})>|(?P<bare>{_EMAIL})"
)
AUTOMATED_ROLES = frozenset({"agent", "bot"})

# What is pseudonymised in each table (documented in docs/dados.md as "Dado pessoal").
PERSONAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "commits": ("author_name", "author_email", "committer_name", "committer_email", "message"),
    "identities": ("identity_key", "name", "email", "github_login"),
    "commit_coauthors": ("name", "email"),
    "pipeline_runs": ("progress",),
    "rq3_author_roles": ("name", "github_login"),
    "rq3_changes": ("message_subject", "message"),
}


@dataclass(frozen=True, slots=True)
class Pseudonymizer:
    salt: bytes
    config: AnalysisConfig

    def __post_init__(self) -> None:
        if len(self.salt) < 16:
            raise ValueError("the pseudonymisation salt needs at least 16 bytes")

    def _digest(self, kind: str, value: str) -> str:
        message = f"{kind}\x1f{value}".encode()
        return hmac.new(self.salt, message, hashlib.sha256).hexdigest()[:16]

    def automated(self, name: str | None, email: str | None) -> bool:
        """A bot or a coding agent, by the patterns the analysis uses."""
        name, email = name or "", email or ""
        if is_bot(name, email, self.config):
            return True
        return detect_agent(name, email, self.config) is not None

    def name(self, value: str | None) -> str | None:
        return f"person-{self._digest('name', value)}" if value else value

    def email(self, value: str | None) -> str | None:
        return f"{self._digest('email', value)}@users.invalid" if value else value

    def login(self, value: str | None) -> str | None:
        return f"user-{self._digest('login', value)}" if value else value

    def identity_key(self, value: str | None) -> str | None:
        return f"id-{self._digest('identity', value)}" if value else value

    def person(self, name: str | None, email: str | None) -> tuple[str | None, str | None]:
        if self.automated(name, email):
            return name, email
        return self.name(name), self.email(email)

    def text(self, value: str | None) -> str | None:
        """``value`` with every person's "Name <address>" and bare address replaced by their
        pseudonyms (names written elsewhere in the text cannot be told apart and stay)."""
        if not value:
            return value

        def replace(match: re.Match[str]) -> str:
            if match.group("email"):
                name, email = match.group("name"), match.group("email")
                if self.automated(name, email):
                    return match.group(0)
                return f"{self.name(name)} <{self.email(email)}>"
            address = match.group("bare")
            return address if self.automated("", address) else self.email(address) or address

        return PERSON_IN_TEXT.sub(replace, value)


def _people(frame: pl.DataFrame, name: str, email: str, p: Pseudonymizer) -> pl.DataFrame:
    pairs = [p.person(n, e) for n, e in zip(frame[name], frame[email], strict=True)]
    return frame.with_columns(
        pl.Series(name, [n for n, _ in pairs], dtype=frame.schema[name]),
        pl.Series(email, [e for _, e in pairs], dtype=frame.schema[email]),
    )


def _mapped(frame: pl.DataFrame, column: str, function: Callable[[str | None], str | None]):
    return frame.with_columns(
        pl.Series(column, [function(v) for v in frame[column]], dtype=frame.schema[column])
    )


def _commits(frame: pl.DataFrame, p: Pseudonymizer) -> pl.DataFrame:
    frame = _people(frame, "author_name", "author_email", p)
    frame = _people(frame, "committer_name", "committer_email", p)
    return _mapped(frame, "message", p.text)


def _identities(frame: pl.DataFrame, p: Pseudonymizer) -> pl.DataFrame:
    keep = [p.automated(n, e) for n, e in zip(frame["name"], frame["email"], strict=True)]

    def column(name: str, function: Callable[[str | None], str | None]) -> pl.Series:
        values = [v if kept else function(v) for v, kept in zip(frame[name], keep, strict=True)]
        return pl.Series(name, values, dtype=frame.schema[name])

    return frame.with_columns(
        column("identity_key", p.identity_key),
        column("name", p.name),
        column("email", p.email),
        column("github_login", p.login),
    )


def _author_roles(frame: pl.DataFrame, p: Pseudonymizer) -> pl.DataFrame:
    keep = [role in AUTOMATED_ROLES for role in frame["role"]]

    def column(name: str, function: Callable[[str | None], str | None]) -> pl.Series:
        values = [v if kept else function(v) for v, kept in zip(frame[name], keep, strict=True)]
        return pl.Series(name, values, dtype=frame.schema[name])

    return frame.with_columns(column("name", p.name), column("github_login", p.login))


def _changes(frame: pl.DataFrame, p: Pseudonymizer) -> pl.DataFrame:
    return _mapped(_mapped(frame, "message_subject", p.text), "message", p.text)


def _runs(frame: pl.DataFrame, p: Pseudonymizer) -> pl.DataFrame:
    return frame.with_columns(pl.lit(None, dtype=frame.schema["progress"]).alias("progress"))


_TRANSFORMS: dict[str, Callable[[pl.DataFrame, Pseudonymizer], pl.DataFrame]] = {
    "commits": _commits,
    "identities": _identities,
    "commit_coauthors": lambda frame, p: _people(frame, "name", "email", p),
    "pipeline_runs": _runs,
    "rq3_author_roles": _author_roles,
    "rq3_changes": _changes,
}
assert set(_TRANSFORMS) == set(PERSONAL_COLUMNS)


def has_personal_data(table: str) -> bool:
    return table in _TRANSFORMS


def anonymize_frame(table: str, frame: pl.DataFrame, p: Pseudonymizer) -> pl.DataFrame:
    """``frame`` (a whole table or a slice of it) with its personal data pseudonymised; tables
    without personal data come back unchanged."""
    transform = _TRANSFORMS.get(table)
    return frame if transform is None else transform(frame, p)
