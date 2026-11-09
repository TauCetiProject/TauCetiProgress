# Commit provenance and historical PR numbers

Progress reads full first-parent commit messages. A `Source-PR` citation selects
its repository and PR number. Queue repair notes may follow the citation; they
do not remove it from attribution. Legacy squash subjects ending in `(#N)` and
`Merge pull request #N` remain supported with a separately supplied legacy
repository. Bare `owner/repository#N` source citations always mean GitHub.

Qualified citations use the queue's existing format:

```text
Source-PR: forgejo:https://forge.example/team/Math#7
```

The forge kind, origin and repository name remain part of the citation. PR 7 on
GitHub and PR 7 on two Forgejo origins are three different objects. Only complete
trailers matching the queue's grammar count; prose and empty or lookalike fields
are ignored. Malformed or conflicting recognized citations, or disagreement with
the subject number, abort the read.
Identical duplicate citations retain the queue chain reader's behavior.

`window.first_parent_sources()` returns qualified citations without converting
their numbers. `window.window_prs()`, `earliest_merged()` and `find_merge_commit()`
accept an explicit `repository`, `legacy_repository` and `migration`. They return
current-repository numbers only after checking the origin or applying that map.
An unqualified historical subject needs an explicit legacy namespace when the
report repository is native. Foreign citations without a matching map refuse;
they never become matching current PR numbers by coincidence.

`MigrationMap.parse()` accepts this versioned, one-to-one correspondence:

```json
{
  "schema": "tauceti-progress.migration/v1",
  "mappings": [
    {
      "from": "github:https://github.com/team/Math#7",
      "to": "forgejo:https://forge.example/team/Math#103"
    }
  ]
}
```

Supply mappings from trusted migration configuration, not PR prose. Duplicate
sources or destinations, same-repository renumbering and mapping chains are
rejected. Distinct citations cannot collapse onto one current number. A map to
another report repository cannot authorize attribution. These citations describe source history; they grant no
review, permission or merge authority.

The existing GitHub planner, bootstrap checks and factual line attribution use
the same reader. Commit bodies can supply missing citations even when a squash
subject carries no number. Planning memoizes local history within one round,
with at most four entries and 100,000 citation rows. Mutable refs are resolved
before lookup, shallow reads are not memoized, and the cache is discarded on
return or failure. No persistent state store is added.

The command-line entry points still use the legacy bootstrap and reject native
project configurations. Native API history collection, plan/facts metadata,
publication/fork handling, announcements and workflow integration must be ported
before enabling that bootstrap. The explicit reader interfaces prepare that
integration without routing current operations to a GitHub mirror.
