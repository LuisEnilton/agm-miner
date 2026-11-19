"""Pseudonymisation of the personal data in the published tables (pure functions).

Names, e-mails, GitHub logins and identity keys of people become stable pseudonyms: an HMAC of
the original value with a secret salt (``PACKAGE_SALT``, never published), so the same person
gets the same pseudonym in every table and the joins still work. Bots and coding agents (the
``analysis`` patterns of ``study.yaml``) keep the name or e-mail that identifies them (e.g.
``dependabot[bot]``, ``noreply@anthropic.com``), so the pattern that matched them still matches
and ``agm analyze`` on the pseudonymised bronze export reproduces the pseudonymised gold tables,
author roles included; the other part is pseudonymised, since people sometimes commit under a
bot's name or with an agent's e-mail. E-mails inside commit messages get the same
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

# An address inside free text (commit messages): local part, @, domain ending in an alphabetic
# top-level domain, not glued to a path or a word. This leaves out package specs
# (``@ai-sdk/provider@3.0.14``), SSH remotes (``git@github.com:org/repo``) and URL paths.
_EMAIL = (
    r"(?<![\w.%+\-/@])[\w.%+\-\[\]]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}"
    r"(?![\w\-/:@])"
)
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

    def address(self, value: str | None) -> str | None:
        """The e-mail of a bot or agent: kept only when it is itself a bot's or an agent's address
        (a person sometimes commits under a bot's name with their own e-mail)."""
        return value if self.automated("", value) else self.email(value)

    def bot_login(self, value: str | None) -> str | None:
        """The GitHub login of a bot or agent: kept only when it is itself a bot's login."""
        return value if self.automated(value, "") else self.login(value)

    def bot_name(self, value: str | None) -> str | None:
        """The name of a bot or agent: kept only when it is itself a bot's or an agent's name
        (someone who commits with an agent's e-mail is still a person with a name)."""
        return value if self.automated(value, "") else self.name(value)

    def person(self, name: str | None, email: str | None) -> tuple[str | None, str | None]:
        """A person gets pseudonyms. A bot or agent keeps the part that identifies it (name,
        e-mail or both), so the pattern that matched it still matches and the analysis sees the
        same bot or agent; the other part is pseudonymised like a person's."""
        if self.automated(name, email):
            return self.bot_name(name), self.address(email)
        return self.name(name), self.email(email)

    def text(self, value: str | None) -> str | None:
        """``value`` with every person's "Name <address>" and bare address replaced by their
        pseudonyms (names written elsewhere in the text cannot be told apart and stay)."""
        if not value:
            return value

        def replace(match: re.Match[str]) -> str:
            if match.group("email"):
                name, email = self.person(match.group("name"), match.group("email"))
                return f"{name} <{email}>"
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
    """The identity key is the normalised e-mail: it is kept exactly when the e-mail is."""
    names, emails, keys, logins = [], [], [], []
    for name, email, key, login in zip(
        frame["name"], frame["email"], frame["identity_key"], frame["github_login"], strict=True
    ):
        automated = p.automated(name, email)
        new_name, new_email = p.person(name, email)
        names.append(new_name)
        emails.append(new_email)
        keys.append(key if automated and new_email == email else p.identity_key(key))
        logins.append(p.bot_login(login) if automated else p.login(login))
    return frame.with_columns(
        pl.Series("name", names, dtype=frame.schema["name"]),
        pl.Series("email", emails, dtype=frame.schema["email"]),
        pl.Series("identity_key", keys, dtype=frame.schema["identity_key"]),
        pl.Series("github_login", logins, dtype=frame.schema["github_login"]),
    )


def _author_roles(frame: pl.DataFrame, p: Pseudonymizer) -> pl.DataFrame:
    """Same rules as ``identities``, with the role telling bots and agents apart (the analysis
    gives them the roles ``bot`` and ``agent`` by the same patterns)."""
    names, logins = [], []
    for name, login, role in zip(frame["name"], frame["github_login"], frame["role"], strict=True):
        automated = role in AUTOMATED_ROLES
        names.append(p.bot_name(name) if automated else p.name(name))
        logins.append(p.bot_login(login) if automated else p.login(login))
    return frame.with_columns(
        pl.Series("name", names, dtype=frame.schema["name"]),
        pl.Series("github_login", logins, dtype=frame.schema["github_login"]),
    )


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
