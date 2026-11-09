"""Bounded streaming of mainline commit citations; no commit bodies are retained."""

import codecs
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass

from .provenance import _TRAILER_RE, ProvenanceError, PullSource, subject_number

CHUNK_BYTES = 64 * 1024
MAX_CITATION_LINE = 8192
_PREFIX = "source-pr:"
_LINE_ENDINGS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"


class HistoryReadError(RuntimeError):
    pass


def environment():
    return {
        **os.environ,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_GRAFT_FILE": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


@dataclass(frozen=True)
class CommitCitation:
    sha: str
    number: int | None
    citation: PullSource | None
    error: str | None


class _Commit:
    def __init__(self, sha):
        self.sha = sha
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.head = self.tail = self.line = ""
        self.drop = self.long = self.value = False
        self.citation = None
        self.error = None

    def text(self, raw, *, body=False, final=False):
        if self.error:
            return
        try:
            text = self.decoder.decode(raw, final=final)
        except UnicodeError:
            self.error = "commit text is not valid UTF-8"
            return
        if not body:
            self.head = (self.head + text)[:256]
            self.tail = (self.tail + text)[-256:]
            return
        for part in text.splitlines(keepends=True):
            ended = part[-1] in _LINE_ENDINGS
            segment = part[:-1] if ended else part
            if not self.drop:
                candidate = self.line + segment
                if len(candidate) >= len(_PREFIX) and not candidate.lower().startswith(_PREFIX):
                    self.drop = True
                    self.line = ""
                elif len(candidate) > len(_PREFIX) and candidate[len(_PREFIX)] not in " \t":
                    self.drop = True
                    self.line = ""
                else:
                    if len(candidate) > len(_PREFIX):
                        value = candidate[len(_PREFIX) :].lstrip(" \t")
                        if value and value[0].isspace():
                            self.drop = True
                            self.line = ""
                            if ended:
                                self.end_line()
                            continue
                        # Separator whitespace is not citation data.
                        candidate = _PREFIX + " " + value
                    self.value = self.value or bool(candidate[len(_PREFIX) :].strip())
                    self.long = self.long or len(candidate) > MAX_CITATION_LINE
                    self.line = candidate[:MAX_CITATION_LINE]
            if ended:
                self.end_line()

    def end_line(self):
        if self.long and self.value:
            self.error = "Source-PR citation exceeds its bound"
        elif not self.drop and not self.long:
            match = _TRAILER_RE.match(self.line)
            if match and match["token"].lower() == "source-pr":
                try:
                    citation = PullSource.parse(match["value"])
                    if self.citation is not None and self.citation != citation:
                        raise ProvenanceError("ambiguous Source-PR citation")
                    self.citation = citation
                except ProvenanceError as error:
                    self.error = str(error)
        self.line = ""
        self.drop = self.long = self.value = False

    def subject_done(self):
        self.text(b"", final=True)
        self.decoder = codecs.getincrementaldecoder("utf-8")()

    def finish(self):
        self.text(b"", body=True, final=True)
        self.end_line()
        return CommitCitation(self.sha, subject_number(self.head + "\n" + self.tail), self.citation, self.error)


def read(repo_dir, revision):
    """Return complete, immutable records or fail; partial Git output is never returned."""
    command = [
        "git",
        "-C",
        str(repo_dir),
        "log",
        "--first-parent",
        "--format=%H%x00%s%x00%B%x00",
        "--end-of-options",
        revision,
        "--",
    ]
    rows = []
    field = 0
    header = bytearray()
    current = None
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, env=environment()) as process:
            try:
                while chunk := process.stdout.read(CHUNK_BYTES):
                    parts = chunk.split(b"\0")
                    for index, part in enumerate(parts):
                        if field == 0:
                            header.extend(part)
                            if len(header) > 128:
                                raise HistoryReadError("invalid first-parent commit framing")
                        else:
                            current.text(part, body=field == 2)
                        if index == len(parts) - 1:
                            continue
                        if field == 0:
                            sha = bytes(header).strip()
                            if re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", sha) is None:
                                raise HistoryReadError("invalid first-parent commit identity")
                            current = _Commit(sha.decode("ascii"))
                            header.clear()
                            field = 1
                        elif field == 1:
                            current.subject_done()
                            field = 2
                        else:
                            rows.append(current.finish())
                            current = None
                            field = 0
                if field or bytes(header).strip():
                    raise HistoryReadError("incomplete first-parent commit framing")
                if process.wait() != 0:
                    raise HistoryReadError("git could not read complete first-parent history")
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.kill()
                    process.wait()
    return rows
