# Single Pages facts bundle fixture

`facts.json` and `sources.json` are copied byte-for-byte from
[TauCeti PR #19012](https://github.com/TauCetiProject/TauCeti/pull/19012), commit
[`0424be258106fab1fc976fe35588a95d74a3e563`](https://github.com/TauCetiProject/TauCeti/tree/0424be258106fab1fc976fe35588a95d74a3e563/scripts/docs_facts_fixture).
They are the exporter's real output and Lean source for a three-module doc-gen4 fixture, using
`leanprover/lean4:v4.36.0-rc1` and doc-gen4 revision
`d25c28427ff9693ea711584a7037da22cdeee1c8`. See the producer's
[fixture notes](https://github.com/TauCetiProject/TauCeti/blob/0424be258106fab1fc976fe35588a95d74a3e563/scripts/docs_facts_fixture/README.md)
and [wire format](https://github.com/TauCetiProject/TauCeti/blob/0424be258106fab1fc976fe35588a95d74a3e563/scripts/docs-facts.md).

The payload contains `schema_version`, `source_commit`, `provenance` (`toolchain`, `doc_gen`),
and a complete `modules` map. Declarations have `kind`, canonical `url`, `file`, inclusive 1-based
`start`/`end` (or both null), and raw `doc` text. The fixture includes generated instances and their
fields, rendered declaration kinds, docstrings, and an empty module. Source links in the producer's
rendered pages can name older analysis commits for byte-identical modules; the payload source is
the documented generation at which the consumer blames those ranges.

The producer publishes only `<docs base>/facts.json.gz`. Tests gzip these exact JSON bytes, validate
the real payload, and derive broken/deployment scenarios from it. Git integration tests commit the
source fixture to a temporary repository and substitute that local commit as `source_commit`,
without parsing declarations or running Lean. There is no manifest, current pointer, immutable
remote URL, R2 base, or GitHub API read in this protocol. Report pins retain compressed bytes in
an origin/source/hash cache on the consumer.
