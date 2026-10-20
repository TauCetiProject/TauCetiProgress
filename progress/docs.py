"""The published documentation facts, read as the authority on what declarations exist.

Deciding what a Lean file declares cannot be done by reading the text. Names are qualified by an
enclosing `namespace`, which interacts with `section`, with `end` closing either, with `open ... in`,
and with `_root_`; and many real declarations are never written down at all -- structure projections,
constructors, instances and `deriving` output are produced during elaboration. A Python
approximation of that gets *most* names right, which is the worst possible outcome: the wrong ones
are indistinguishable from the right ones, and a documentation link built from a wrong name is a
plausible-looking dead link. The first version of this code produced
`ContinuousLinearMap.IsFredholm.of_continuousLinearEquiv` for a declaration the compiler calls
`TauCeti.IsFredholm.of_continuousLinearEquiv`.

doc-gen4 already computes the answer from the elaborated environment, and TauCeti's docs build
exports it as one file, `<docs base>/facts.json.gz`, deployed beside the HTML it describes: every
documented module with its declarations' names, kinds, canonical links, source ranges and
docstrings, at one source commit. One request returns one build, so a report can never mix two
deployments. There is no other reader: a site without the file has no facts to offer, and the run
waits for one.

The site serves only the current file. A plan retains the exact bytes it selected (keyed by origin,
source and hash, and as a sibling of `plan --out`), so facts read the same generation after a later
deploy; when no verified copy remains, the window is replanned without moving any cursor.
"""

import gzip
import hashlib
import http.client
import json
import os
import pathlib
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib

DOCS_BASE = os.environ.get("TAUCETI_DOCS_URL", "https://taucetiproject.org/docs")


class DocsError(RuntimeError):
    """The documentation facts could not be read."""


class DocsGenerationChanged(DocsError):
    """Positive evidence of different documentation builds; retry plan AND facts together.

    Consumers must not retry paid writing or publish any part of an incoherent window.
    """

    def __init__(self, expected, observed, page=None):
        self.expected = expected
        self.observed = observed
        self.page = page
        super().__init__(
            f"{page or 'the documentation'} was built from {observed[:7]}, not {expected[:7]}; "
            "the site is redeploying; replan the window before collecting facts "
            f"(expected={expected} observed={observed} page={page or '-'})"
        )


class ArtifactError(DocsError):
    """Retriable facts infrastructure failure; retain artifacts and do not move the cursor."""


class ArtifactNotFound(ArtifactError):
    """An artifact URL returned HTTP 404 (distinct from a transport outage)."""


class UnsupportedFactsSchema(ArtifactError):
    """An explicitly versioned bundle is newer than this consumer."""


class ReplanRequired(ArtifactError):
    """The saved source/window identity is invalid; retain artifacts for safe replanning."""


class FactsReplanRequired(ArtifactError):
    """The sole pinned copy is gone and fresh source selection is needed, without moving a cursor."""


class ConfigurationError(ValueError):
    """The local facts-reader configuration must be corrected."""


class FactsGenerationChanged(DocsGenerationChanged):
    """The mutable bundle no longer holds a saved plan's exact bytes; replan mechanically."""

    def __init__(self, pin, source, digest):
        super().__init__(pin["source_commit"], source, "facts.json.gz")
        self.expected_sha256 = pin["facts_sha256"]
        self.observed_sha256 = digest
        self.args = (f"facts.json.gz changed: expected source={self.expected} "
                     f"sha256={self.expected_sha256}, observed source={self.observed} "
                     f"sha256={digest}; saved copy unavailable; replan the unadvanced window",)


def _require(condition, message):
    if not condition:
        raise ArtifactError(message)


def _sha(value, length):
    return isinstance(value, str) and re.fullmatch(f"[0-9a-f]{{{length}}}", value) is not None


def _json(raw, label):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, f"{label}: duplicate JSON key {key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise ArtifactError(f"{label}: invalid JSON: {exc}") from exc
    _require(isinstance(value, dict), f"{label}: expected an object")
    return value


def _schema(value, label):
    _require(type(value.get("schema_version")) is int and value["schema_version"] == 1,
             f"{label}: unsupported schema_version")


def _source_path(value):
    return (isinstance(value, str) and value.endswith(".lean")
            and not value.startswith("/") and "\\" not in value
            and all(p not in ("", ".", "..") for p in value.split("/")))


def _url(value, label):
    try:
        return urllib.parse.urlsplit(value if isinstance(value, str) else "")
    except ValueError as exc:
        raise ArtifactError(f"{label}: invalid URL: {exc}") from exc


def store(path, raw):
    """Atomically and durably write `raw` to `path`; False when the location is not writable."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp, path)
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            pathlib.Path(tmp).unlink(missing_ok=True)
        return True
    except OSError:
        return False


class Docs:
    """One hash-verified `facts.json.gz`, discovered live or restored from a saved plan's pin."""

    def __init__(self, base=None, pin=None, cache_dir=None, opener=None, raw=None, bundle_path=None):
        self._restoring = pin is not None
        try:
            if pin is not None:
                _require(isinstance(pin, dict), "invalid saved facts bundle pin")
                _schema(pin, "pin")
                _require(_sha(pin.get("source_commit"), 40) and _sha(pin.get("facts_sha256"), 64),
                         "invalid saved facts bundle identity")
                _require("coverage" in pin and "generation" in pin, "incomplete saved facts bundle pin")
                base = pin.get("docs_base")
            parsed = _url(base, "docs_base")
            _require(parsed.scheme in ("http", "https") and parsed.netloc
                     and not parsed.query and not parsed.fragment and not parsed.username,
                     "invalid docs_base")
        except ArtifactError as exc:
            if pin is not None:
                raise ReplanRequired(str(exc)) from exc
            raise ConfigurationError(str(exc)) from exc
        self.base = base.rstrip("/")
        self._bundle_file = pin.get("bundle_file") if pin else None
        self._opener = opener or self._fetch
        state = pathlib.Path(os.environ.get("XDG_STATE_HOME") or pathlib.Path.home() / ".local/state")
        root = pathlib.Path(cache_dir or os.environ.get("TAUCETI_FACTS_CACHE")
                            or state / "tauceti-progress/facts")
        origin = root / hashlib.sha256(self.base.encode()).hexdigest() / "pages"
        self._retained = False
        if pin is not None:
            cache_path = origin / pin["source_commit"] / pin["facts_sha256"] / "facts.json.gz"
            for path in (bundle_path, cache_path):
                if path is not None:
                    cached = self._verified_copy(pathlib.Path(path), pin["facts_sha256"])
                    if cached is not None:
                        raw = cached
                        self._retained = True
                        break
        if raw is None:
            try:
                raw = self._opener(self.base + "/facts.json.gz")
            except ArtifactNotFound as exc:
                if pin is not None:
                    raise FactsReplanRequired(
                        f"pinned facts unavailable: source={pin['source_commit']} "
                        f"sha256={pin['facts_sha256']}; live bundle returned 404; "
                        "replan the unadvanced window") from exc
                raise
        _require(isinstance(raw, bytes), "facts.json.gz: expected binary response")
        digest = hashlib.sha256(raw).hexdigest()
        try:
            try:
                data = _json(gzip.decompress(raw), "facts.json.gz")
            except (OSError, EOFError, zlib.error) as exc:
                raise ArtifactError(f"facts.json.gz: truncated or invalid gzip: {exc}") from exc
            version = data.get("schema_version")
            _require(type(version) is int, "facts.json.gz: invalid schema_version")
        except ArtifactError as exc:
            if pin is not None and digest == pin["facts_sha256"]:
                raise ReplanRequired(f"invalid hash-verified saved facts: {exc}") from exc
            raise
        # A complete, differently hashed response cannot replace this pin. Replanning discovers
        # afresh, while a corrupt/truncated transfer above remains a transport/integrity wait.
        if pin is not None and digest != pin["facts_sha256"]:
            if version == 1 and _sha(data.get("source_commit"), 40):
                raise FactsGenerationChanged(pin, data["source_commit"], digest)
            raise FactsReplanRequired(
                f"pinned facts unavailable: source={pin['source_commit']} sha256={pin['facts_sha256']}; "
                f"live bundle sha256={digest} schema={version}; replan the unadvanced window")
        try:
            if version != 1:
                raise UnsupportedFactsSchema(f"facts.json.gz advertises unsupported schema {version}")
            self.commit = data.get("source_commit")
            _require(_sha(self.commit, 40), "facts.json.gz: invalid source_commit")
            provenance = data.get("provenance")
            _require(isinstance(provenance, dict) and all(isinstance(provenance.get(k), str)
                     and provenance[k] for k in ("toolchain", "doc_gen")),
                     "facts.json.gz: missing toolchain/doc-gen provenance")
            modules = data.get("modules")
            _require(isinstance(modules, dict) and modules and all(_source_path(p) for p in modules),
                     "facts.json.gz: invalid module coverage")
            self.modules = sorted(modules)
            self.facts_hash = digest
            self.generation = f"pages/{self.commit}/{digest}"
            self._load_modules(modules)
            if pin is not None:
                _require(self.commit == pin["source_commit"],
                         "saved source commit contradicts the pinned facts hash")
                _require(pin["coverage"] == self.modules, "pin: module coverage changed")
                _require(pin["generation"] == self.generation, "pin: generation mismatch")
        except ArtifactError as exc:
            if pin is not None:
                raise ReplanRequired(str(exc)) from exc
            raise
        self._raw = raw
        self.cache_dir = origin / self.commit / digest

    def _load_modules(self, modules):
        self._modules = modules
        names = set()
        for path, declarations in modules.items():
            _require(isinstance(declarations, dict), f"facts: invalid module {path}")
            for name, info in declarations.items():
                _require(isinstance(name, str) and bool(name) and name not in names,
                         "facts: empty or duplicate declaration name")
                names.add(name)
                _require(isinstance(info, dict), f"facts: invalid declaration {name}")
                _require(isinstance(info.get("kind"), str) and bool(info["kind"])
                         and isinstance(info.get("doc"), str), f"facts: invalid kind/doc for {name}")
                url = _url(info.get("url"), f"facts URL for {name}")
                _require(url.scheme in ("http", "https") and bool(url.netloc) and bool(url.fragment),
                         f"facts: invalid canonical URL for {name}")
                suffix = "/" + path[:-5] + ".html"
                _require((url.path.endswith(suffix) or urllib.parse.unquote(url.path).endswith(suffix))
                         and (url.fragment == name or urllib.parse.unquote(url.fragment) == name),
                         f"facts: canonical URL does not match module/declaration {name}")
                _require(info.get("file") == path, f"facts: source file mismatch for {name}")
                start, end = info.get("start"), info.get("end")
                _require("start" in info and "end" in info and
                         ((start is None and end is None) or
                          (type(start) is int and type(end) is int and 1 <= start <= end)),
                         f"facts: invalid source range for {name}")

    @staticmethod
    def _fetch(url):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            error = ArtifactNotFound if exc.code == 404 else ArtifactError
            exc.close()
            raise error(f"fetching {url} failed: {exc}") from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            raise ArtifactError(f"fetching {url} failed: {exc}") from exc

    @staticmethod
    def _verified_copy(path, digest):
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        return raw if hashlib.sha256(raw).hexdigest() == digest else None

    def retain(self, path=None):
        """Retain only selected plans. Explicit paths tie a bundle's lifetime to its plan artifact."""
        if path is None and self._retained:
            return
        path = pathlib.Path(path) if path is not None else self.cache_dir / "facts.json.gz"
        if self._verified_copy(path, self.facts_hash) is None:
            _require(store(path, self._raw), f"cannot retain pinned facts at {path}")
            _require(self._verified_copy(path, self.facts_hash) is not None,
                     f"cannot retain complete pinned facts at {path}")
        self._retained = True

    def source_commit(self):
        return self.commit

    def require_coverage(self, paths):
        missing = sorted(set(paths) - set(self.modules))
        if missing:
            error = ReplanRequired if self._restoring else ArtifactError
            raise error(f"generation {self.generation}: missing required modules: {missing}")

    def declarations(self, module_page):
        path = module_page.removesuffix(".html") + ".lean"
        self.require_coverage([path])
        return {name: {**info, "commit": self.commit} for name, info in self._modules[path].items()}

    def pin(self):
        self.retain()
        return {"schema_version": 1, "docs_base": self.base,
                "source_commit": self.commit, "generation": self.generation,
                "facts_sha256": self.facts_hash, "coverage": self.modules,
                **({"bundle_file": self._bundle_file} if self._bundle_file else {})}


def discover(base=None, cache_dir=None, opener=None):
    """The live bundle, fetched afresh for each plan; never a stale disk discovery result."""
    return Docs(base=base or DOCS_BASE, cache_dir=cache_dir, opener=opener)
