"""Version 1 immutable doc-gen facts. No HTML or provider API reads after selection.

The pointer is discovery only. A saved pin contains everything needed to retrieve the same
manifest and bundle without consulting current.json, including after a later deployment.
"""

import gzip
import hashlib
import http.client
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib

from .docs import Docs, DocsError


class ArtifactError(DocsError):
    """Retriable facts infrastructure failure; retain artifacts and do not move the cursor."""


class ArtifactNotFound(ArtifactError):
    """An artifact URL returned HTTP 404 (distinct from a transport outage)."""


class ReplanRequired(ArtifactError):
    """The saved source/window identity is invalid; retain artifacts for safe replanning."""


class ConfigurationError(ValueError):
    """The local facts-reader configuration must be corrected."""


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


def _coverage(value):
    _require(isinstance(value, list) and all(_source_path(p) for p in value),
             "manifest: invalid module coverage")
    _require(len(value) == len(set(value)), "manifest: duplicate module coverage")
    return sorted(value)


class FactsDocs:
    """A hash-verified compiler-derived generation, exposing the legacy reader's interface."""

    def __init__(self, pin, cache_dir=None, opener=None):
        _require(isinstance(pin, dict), "invalid saved documentation pin")
        _schema(pin, "pin")
        _require(pin.get("mode") == "immutable", "invalid documentation mode")
        self.base = pin.get("facts_base")
        parsed = _url(self.base, "pin facts_base")
        _require(parsed.scheme in ("http", "https") and parsed.netloc
                 and not parsed.query and not parsed.fragment and not parsed.username,
                 "pin: invalid facts_base")
        self.base = self.base.rstrip("/")
        self.commit = pin.get("source_commit")
        self.manifest_hash = pin.get("manifest_sha256")
        _require(_sha(self.commit, 40) and _sha(self.manifest_hash, 64),
                 "pin: invalid source commit or manifest hash")
        self.manifest_path = pin.get("manifest_path")
        _require(isinstance(self.manifest_path, str), "pin: invalid manifest path")
        match = re.fullmatch(r"v1/generations/([0-9a-f]{40})/([0-9a-f]{64})/manifest\.json",
                             self.manifest_path or "")
        _require(match is not None and match[1] == self.commit,
                 "pin: manifest path does not match source commit")
        self.generation = self.manifest_path.rsplit("/", 1)[0]
        self.facts_hash = match[2]
        self._opener = opener or self._fetch
        root = pathlib.Path(cache_dir or os.environ.get("TAUCETI_FACTS_CACHE")
                            or "/tmp/tauceti-facts-cache")
        self.cache_dir = root / hashlib.sha256(self.base.encode()).hexdigest() / self.generation
        self.manifest = _json(self._get("manifest.json", self.manifest_hash), "manifest")
        _schema(self.manifest, "manifest")
        _require(self.manifest.get("source_commit") == self.commit
                 and self.manifest.get("generation") == self.generation,
                 "manifest: source commit/generation mismatch")
        _require(self.manifest.get("facts_sha256") == self.facts_hash,
                 "manifest: facts hash does not match generation path")
        self.modules = _coverage(self.manifest.get("modules"))
        _require(isinstance(self.manifest.get("provenance"), dict)
                 and all(isinstance(self.manifest["provenance"].get(k), str)
                         and self.manifest["provenance"][k] for k in ("toolchain", "doc_gen")),
                 "manifest: missing toolchain/doc-gen provenance")
        for key in ("compressed_bytes", "uncompressed_bytes"):
            _require(type(self.manifest.get(key)) is int and self.manifest[key] > 0,
                     f"manifest: invalid {key}")
        raw = self._get("facts.json.gz", self.facts_hash)
        _require(len(raw) == self.manifest["compressed_bytes"], "facts: compressed length mismatch")
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                unpacked = stream.read(self.manifest["uncompressed_bytes"] + 1)
        except (OSError, EOFError, zlib.error) as exc:
            raise ArtifactError(f"facts: truncated or invalid gzip: {exc}") from exc
        _require(len(unpacked) == self.manifest["uncompressed_bytes"],
                 "facts: uncompressed length mismatch")
        data = _json(unpacked, "facts")
        _schema(data, "facts")
        _require(data.get("source_commit") == self.commit, "facts: source commit mismatch")
        self._modules = data.get("modules")
        _require(isinstance(self._modules, dict) and sorted(self._modules) == self.modules,
                 "facts: module coverage differs from manifest")
        names = set()
        for path, declarations in self._modules.items():
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
        if "coverage" in pin:
            _require(pin["coverage"] == self.modules, "pin: module coverage changed")
        if "generation" in pin:
            _require(pin["generation"] == self.generation, "pin: generation mismatch")

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

    def _get(self, name, digest):
        path = self.cache_dir / f"{digest}-{name}"
        try:
            raw = path.read_bytes()
        except OSError:
            raw = None
        if raw is None or hashlib.sha256(raw).hexdigest() != digest:
            raw = self._opener(f"{self.base}/{self.generation}/{name}")
            _require(isinstance(raw, bytes) and hashlib.sha256(raw).hexdigest() == digest,
                     f"{self.generation}/{name}: SHA-256 mismatch")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
                try:
                    with os.fdopen(fd, "wb") as output:
                        output.write(raw)
                    os.replace(tmp, path)
                finally:
                    pathlib.Path(tmp).unlink(missing_ok=True)
            except OSError:
                pass  # read-only caches do not invalidate verified data
        return raw

    def source_commit(self):
        return self.commit

    def require_coverage(self, paths):
        missing = sorted(set(paths) - set(self.modules))
        _require(not missing, f"generation {self.generation}: missing required modules: {missing}")

    def declarations(self, module_page):
        path = module_page.removesuffix(".html") + ".lean"
        self.require_coverage([path])
        return {name: {**info, "commit": self.commit} for name, info in self._modules[path].items()}

    def pin(self):
        return {"mode": "immutable", "schema_version": 1, "facts_base": self.base,
                "source_commit": self.commit, "generation": self.generation,
                "manifest_path": self.manifest_path, "manifest_sha256": self.manifest_hash,
                "coverage": self.modules}


def discover(facts_base=None, cache_dir=None, opener=None):
    """Use legacy until a supported pointer is advertised. Never fall back after selecting v1.

    Only pointer absence (404) or an explicitly unsupported version permit discovery
    fallback. A supported pointer with broken fields, manifest, or bundle is an infrastructure error.
    TAUCETI_FACTS_MODE=immutable forbids fallback; legacy is the rollback switch for new plans.
    """
    mode = os.environ.get("TAUCETI_FACTS_MODE", "auto")
    if mode not in ("auto", "legacy", "immutable"):
        raise ConfigurationError(f"unknown TAUCETI_FACTS_MODE: {mode}")
    base = facts_base or os.environ.get("TAUCETI_FACTS_BASE")
    if mode == "immutable" and not base:
        raise ConfigurationError("TAUCETI_FACTS_MODE=immutable requires TAUCETI_FACTS_BASE")
    if mode == "legacy" or not base:
        return Docs()

    def fallback(reason):
        if mode == "immutable":
            raise ArtifactError(f"{reason}; immutable facts are required")
        print(f"documentation: {reason}; using strict legacy reader", file=sys.stderr)
        return Docs()

    fetch = opener or FactsDocs._fetch
    try:
        raw = fetch(base.rstrip("/") + "/v1/current.json")
    except ArtifactNotFound:
        return fallback("current.json is absent (HTTP 404)")
    pointer = _json(raw, "current.json")
    _require(type(pointer.get("schema_version")) is int, "current.json: invalid schema_version")
    if pointer["schema_version"] != 1:
        return fallback(f"current.json advertises unsupported schema {pointer['schema_version']}")
    _schema(pointer, "current.json")
    return FactsDocs({**pointer, "mode": "immutable", "facts_base": base},
                     cache_dir=cache_dir, opener=opener)
