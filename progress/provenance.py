"""PR attribution from mainline commits, with an explicit namespace for history.

These are source citations, not review or merge authority. Imported PR numbers
only become current-source numbers through a supplied migration map.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from tauceti_tools.forge import GITHUB, Forge, RepositoryRef

_TRAILER_RE = re.compile(r"^(?P<token>[A-Za-z][A-Za-z0-9-]*):[ \t]+(?P<value>\S.*?)\s*$")


class ProvenanceError(ValueError):
    """Commit attribution is ambiguous; no report cursor may advance."""


@dataclass(frozen=True, eq=False)
class PullSource:
    repository: RepositoryRef
    number: int

    def __post_init__(self):
        if (
            not isinstance(self.repository, RepositoryRef)
            or type(self.number) is not int
            or not 0 < self.number < 2**63
        ):
            raise ProvenanceError("source citation needs a qualified repository and positive PR number")

    @property
    def key(self):
        return f"{self.repository.key}#{self.number}"

    def __hash__(self):
        return hash(self.key)

    def __eq__(self, other):
        return self.key == other.key if isinstance(other, PullSource) else NotImplemented

    @classmethod
    def parse(cls, value):
        try:
            if not isinstance(value, str):
                raise TypeError
            repository, separator, number = value.rpartition("#")
            if not separator or re.fullmatch(r"[1-9][0-9]*", number) is None:
                raise ValueError
            if repository.startswith(("github:", "forgejo:")):
                kind, _, location = repository.partition(":")
                parsed = urlsplit(location)
                if "?" in location or "#" in location or parsed.path.count("/") != 2:
                    raise ValueError
                forge = Forge(kind, parsed.scheme + "://" + parsed.netloc)
                reference = RepositoryRef(forge, parsed.path.removeprefix("/"))
            else:
                reference = RepositoryRef(GITHUB, repository)
            return cls(reference, int(number))
        except (ValueError, TypeError):
            raise ProvenanceError("invalid Source-PR citation") from None


def subject_number(subject):
    """Historical GitHub squash or merge subject; its namespace is supplied separately."""
    match = re.search(r"\(#(\d+)\)\s*\Z", subject) or re.match(r"\AMerge pull request #(\d+)\b", subject)
    return int(match.group(1)) if match else None


def commit_source(message, *, legacy_repository=None, subject=None):
    """One source citation, preserving repaired messages whose trailers precede notes.

    Queue messages retain their Source-PR line when repair notes are appended, so
    inspect the entire message. Conflicting or malformed source lines refuse
    rather than falling back to the unqualified subject. Identical duplicates
    retain the queue chain reader's semantics.
    """
    values = []
    lines = message.splitlines()
    for line in lines:
        match = _TRAILER_RE.match(line)
        if match and match["token"].lower() == "source-pr":
            values.append(match["value"])
    # Real history callers supply git's %s: it joins the entire first paragraph.
    number = subject_number(subject if subject is not None else lines[0] if lines else "")
    if values:
        citations = set()
        for value in values:
            citations.add(PullSource.parse(value))
        if len(citations) != 1:
            raise ProvenanceError("ambiguous Source-PR citation")
        citation = citations.pop()
        if number is not None and number != citation.number:
            raise ProvenanceError("merge subject conflicts with Source-PR citation")
        return citation
    if number is None:
        return None
    if not isinstance(legacy_repository, RepositoryRef):
        raise ProvenanceError("an explicit legacy repository is required for unqualified merge subjects")
    return PullSource(legacy_repository, number)


class MigrationMap:
    """An explicit one-to-one import/rename map; no number-preserving assumption."""

    def __init__(self, rows=()):
        self._mapping = {}
        targets = set()
        for before, after in rows:
            if not isinstance(before, PullSource) or not isinstance(after, PullSource):
                raise ProvenanceError("migration entries must be qualified source citations")
            if before in self._mapping or after in targets:
                raise ProvenanceError("migration map contains duplicate source or destination citations")
            if before.repository.key == after.repository.key:
                raise ProvenanceError("migration entries must cross repository namespaces")
            self._mapping[before] = after
            targets.add(after)
        if set(self._mapping) & targets:
            raise ProvenanceError("migration destinations must not be migration sources")

    @classmethod
    def parse(cls, row):
        if (
            not isinstance(row, dict)
            or set(row) != {"schema", "mappings"}
            or row["schema"] != "tauceti-progress.migration/v1"
        ):
            raise ProvenanceError("invalid progress migration schema")
        if not isinstance(row["mappings"], list):
            raise ProvenanceError("invalid progress migration entries")
        entries = []
        for pair in row["mappings"]:
            if not isinstance(pair, dict) or set(pair) != {"from", "to"}:
                raise ProvenanceError("invalid progress migration entry")
            entries.append((PullSource.parse(pair["from"]), PullSource.parse(pair["to"])))
        return cls(entries)

    def number(self, citation, repository):
        if citation.repository.key == repository.key:
            return citation.number
        mapped = self._mapping.get(citation)
        if mapped is None or mapped.repository.key != repository.key:
            raise ProvenanceError("foreign source citation needs an explicit migration to the report repository")
        return mapped.number
