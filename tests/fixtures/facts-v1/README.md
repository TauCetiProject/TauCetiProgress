# Immutable facts v1 contract fixture

This historical fixture tests saved-plan compatibility with Progress PR #33's manifest protocol.
New plans use the [single Pages bundle](../pages-facts/README.md) from TauCeti PR #19012 and
never request this pointer or discover remote generations. Keep these artifacts so existing
saved manifest plans and caches remain testable.
The files under `v1/` can be served unchanged from any HTTP facts base. `facts.json` is the
uncompressed reference payload. All JSON is UTF-8. Hashes cover the exact published bytes.

`v1/current.json` is the only mutable object, with:

- `schema_version`: integer `1`.
- `source_commit`: full 40-character lowercase Git SHA of the documented source.
- `manifest_path`: `v1/generations/<source_commit>/<facts_sha256>/manifest.json`, relative to the facts base.
- `manifest_sha256`: lowercase SHA-256 of the manifest bytes.

The manifest has `schema_version`, `source_commit`, `generation` (the directory containing the
manifest), `facts_sha256` (SHA-256 of **compressed** bytes), `compressed_bytes`, `uncompressed_bytes`,
`modules` (unique source paths, including declaration-free modules), and `provenance` with nonempty
`toolchain` and `doc_gen` strings. The sibling bundle is named `facts.json.gz`. Gzip mtime is zero
for fixture reproducibility; consumers accept any gzip encoding with the advertised hash.

The bundle has `schema_version`, `source_commit`, and `modules`, a map from each covered source
path to its declarations. Each declaration is keyed by its compiler-derived full name and has
`kind`, `url` (the existing canonical documentation URL), `file`, `start`, `end`, and `doc` (plain
source documentation text). Lines are inclusive and 1-based. `start` and `end` may both be null
for compiler-generated declarations without a source location. `file` always identifies its
module source. Empty modules have `{}`. Manifest and bundle coverage must agree exactly.

The fixture deliberately includes a normal declaration, a compiler-generated instance without an
ordinary source range, and an empty module. `tests/test_immutable_docs.py` derives deployment and
failure scenarios from this fixture, including a removed module at a later source commit. The
fixture commit (`aaaa…`) is synthetic: integration tests replace it with a real local Git commit.
No test contacts GitHub or the deployed documentation service. The documented source commit must
be reachable from the configured reporting ref (`origin/docgen` by default); D0 must publish the
real documented source and coordinate that ref before switching the pointer. Canonical URLs must
name the module HTML page and declaration fragment; consumers retain the URLs from facts even
when the live documentation base has changed.
