# Commit provenance and historical PR numbers

Progress streams first-parent commit messages. A `Source-PR` citation selects
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
another report repository cannot authorize attribution. These citations describe
source history; they grant no review, permission or merge authority.

An already-landed ambiguous commit can be repaired in the same record with an
optional `commits` object. Each key is its full immutable SHA; each value is a
qualified citation, or explicit `null` to exclude that commit from PR attribution.
An override takes precedence over the commit's own citation and subject. It
changes no Git objects. Use this only as reviewed operator configuration.

Set `TAUCETI_PROGRESS_HISTORY_MAP` to the absolute path of that JSON file. It is
bounded to 16 MiB and rejects duplicate JSON keys. The planner snapshots it once
per round. `plan --out FILE` retains the record as a content-named sibling,
`FILE.history-<sha256>.json`, using the same atomic writer as existing facts
artifacts. The small plan contains only `history_migration_sha256` and
`history_migration_file`. Keep the sibling with the plan when moving retained
artifacts. A nonempty policy requires `--out`. The facts command restores and
verifies that snapshot, even if the original configuration changed or disappeared.
A missing sibling requests a mechanical replan (76); corruption or a digest
mismatch requires retained-state recovery (78). Apply verifies it again and
returns 78 for either failure, preserving paid output.
A legacy plan lacking the snapshot requires a mechanical replan (exit 76) when a
nonempty map is configured. The canonical policy digest is recorded in the plan
and generated PR metadata. The merge gate compares that claim with the digest
from its independently prepared policy; PR data never chooses the policy.
Historical reports without a digest remain valid under the empty policy.

The reusable merge workflow accepts `history_migration_repository`,
`history_migration_ref` (a full immutable commit), and `history_migration_path`.
Set these in the protected caller workflow, never from pull-request data. The
read-only validation job checks out that GitHub policy repository, refuses
escaping/symlinked data paths, and prepares its own canonical snapshot. The policy
repository must be public or the caller's own repository, readable by that job's
`GITHUB_TOKEN`. Reviewers of protected caller changes must verify that the pinned
commit belongs to the intended protected policy history: a full SHA fixes bytes,
but GitHub can also serve objects from other forks in the repository network.
Dependencies and validation run in a job-private virtual environment. A separate
configured `tools_ref` is used only for configuration loading; its snapshot must
be readable by the validator's pinned Tools version (currently 2177f5f). Defaults
use an empty policy and need no extra checkout. This supports large migration
tables without putting their JSON into a process environment variable.

Coordinate the worker and merge-workflow policy pins before using a nonempty
map. Worker saved-plan and parked-work identities must include the policy
digest before its Progress pin is upgraded for that use. Existing paid outputs
must not be reinterpreted under another map. After coordinating policy pins,
close and mechanically replan reports still in flight under the previous digest;
otherwise the gate refuses them until normal stale-report recovery runs. Preserve
their paid outputs for inspection. The digest checks agreement with independently
loaded policy, not authorship of the PR body. A Tools upgrade that changes
repository-name normalization also changes this digest and needs coordination.

The existing GitHub planner, bootstrap checks and factual line attribution use
the same reader. Commit bodies can supply missing citations even when a squash
subject carries no number. Planning, facts and gate bootstrap memoize local
history within their round, with at most four entries and 100,000 commit records. Mutable refs are resolved
before lookup, shallow reads are not memoized, and the cache is discarded on
return or failure. Streaming retains subject-number context and recognized
citations, discarding unrelated bodies. Citation content is bounded to 8,192
characters; separator whitespace is discarded. Invalid encoding names the
affected commit. Local replacement refs and grafts do not alter attribution.
No persistent state store is added.

The command-line entry points still use the legacy bootstrap and reject native
project configurations. Native API history collection, qualified repository metadata in plans/facts,
publication/fork handling, announcements and workflow integration must be ported
before enabling that bootstrap. The explicit reader interfaces prepare that
integration without routing current operations to a GitHub mirror.
