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
`FILE.history-<sha256>.json` for a nonempty policy, using the same atomic writer as existing facts
artifacts. The small plan contains only `history_migration_sha256` and
`history_migration_file`. Empty policies are reconstructed from their fixed digest
and need no sibling. Keep a nonempty policy sibling with the plan when moving retained
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
map. Worker active preparation and writer-input identities include the policy
digest. A parked window stays blocked across policy changes to avoid paying for
it again; this does not reuse its facts under the new policy. Existing paid outputs
must not be reinterpreted under another map. After coordinating policy pins,
close and mechanically replan reports still in flight under the previous digest;
otherwise the gate refuses them until normal stale-report recovery runs. Preserve
their paid outputs for inspection. The digest checks agreement with independently
loaded policy, not authorship of the PR body. A Tools upgrade that changes
repository-name normalization also changes nonempty policy digests and needs coordination.
The validation runner needs Python with venv/ensurepip support and access to install
the pinned dependencies. Malformed or duplicate PR metadata markers are refused.

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

`native_source.NativeRepository` binds each direct Forgejo API client to an
explicit repository and snapshots its native repository ID. `NativeSource`
provides content and roadmap bindings to `plan.build_plan(source=...)`. Its
history reader resolves relevant label IDs and streams their union from all PR
states, including closed queue contributions, native merges, and reopened work
already present on main. Git decides which candidates actually landed. Repository/shared labels are resolved with complete pagination, including
identically named labels with distinct IDs. When the shared catalog is hidden or
forbidden, the reader uses the complete authorized repository history instead of
assuming the roadmap has no labels. Large unions use bounded batches rather
than repeating the full repository history for each roadmap.
Missing merge metadata, labels, permissions or incomplete pages abort planning.
Reading every state prevents close/reopen events from changing scan membership.
Counted pagination refuses changing totals when other membership changes would
shift pages. Forgejo 17's creation-order sort breaks timestamp ties by issue ID.
These are bounded observations, not an atomic snapshot of concurrently edited
labels. API merge SHAs reachable in the selected window must have corresponding
first-parent PR attribution, including merges nested on a side branch. A merge
SHA on the first-parent chain must match that exact commit's attribution.
This applies to GitHub and Forgejo, even after an area has a cursor; an unrecognized merge needs
an approved commit override before another report can advance past it.
An affected area aborts planning for all areas rather than advancing its cursor. Before upgrading
the production Progress pin, audit current windows and the complete bootstrap
histories of never-reported areas for this refusal and recover
ambiguous commits through reviewed policy; the planner never rewinds existing cursors.
An override on a nested, off-first-parent SHA can explicitly exclude that commit;
it cannot credit it as a separate first-parent contribution. Do not exclude valid
contributions merely to clear the refusal without deciding how they should be reported.
Native in-flight ownership uses authenticated user and repository IDs rather
than display logins. It trusts the canonical roadmap repository or a fork owned
by the authenticated user; another organization-owned repository is not inferred
to be the canonical source. Clients never select credentials or shared-service fallbacks.

Native plans retain the qualified content/roadmap identities and an explicit
legacy-history namespace in `source` (`tauceti-progress.source/v1`). A legacy
namespace requires `legacy_through`, the full immutable last imported commit SHA.
Bare subjects on that commit's first-parent history use the legacy namespace;
bare subjects after it use the current native repository. This prevents a newly
allocated native number from being interpreted through an old renumbering map.
The cutover must belong to the observed first-parent history; incomplete or
divergent histories refuse. Older retained windows before cutover remain valid.
Without an explicit legacy namespace and cutover, bare native subjects remain
ambiguous and refuse; qualified `Source-PR` citations work without that configuration.
The configured cutover explicitly authorizes the namespace of later bare subjects.
Facts restores that binding through `source_record`, including the source origin in its output. This retained metadata identifies the
namespace used for the calculation; it does not independently prove the origin
of a caller-supplied checkout. Trusted callers remain responsible for fetching the
configured repository and immutable source objects.
Current numbers are still selected through qualified Git citations and the
approved migration policy. Git blame follows the first-parent chain so lines
introduced through a native merge are credited to the merge that landed on main,
rather than to an unlabelled feature-branch commit. A native merge with no
recognizable source citation remains subject to the existing refusal/explicit
commit-override rule.
First-parent blame also changes GitHub merge attribution. Before a production
pin upgrade, mechanically regenerate facts for unfinished windows under the new
version and retain existing paid prose for inspection; do not silently reuse old
facts as if calculated by the new version. Qualified writer context requires the
same plan/facts source binding, window endpoints, documentation pin and PR set,
plus complete, non-duplicated qualified commentary from its content repository.

Qualified generated status/section headers retain the source binding. A log
cannot silently downgrade or switch that binding; appending to an imported
unqualified log requires the explicit legacy namespace/cutover. Qualified PR
descriptions and announcement links select the configured repository origin.
Announcement IDs include both qualified repositories and full window commits,
while historical unqualified sections retain their existing IDs. The merge gate
requires an independently supplied source binding matching both files and PR
metadata; a PR cannot select its own source configuration.

`native_publish.NativePublisher` accepts an explicitly constructed `NativeSource`,
the roadmap token and its expected native actor ID. It revalidates content,
roadmap and actor identities, fetches the configured roadmap directly, and uses
the same local report preparation as the GitHub publisher. Native fork discovery
uses owned repository IDs and parent ancestry, including renamed forks. Pushes
create a branch only when absent; existing remote candidates are fetched and
validated without overwriting them. PR creation, fork creation and cleanup use
the existing private publication outbox, keyed by origin, actor and operation.
Native success is acknowledged through authoritative reads. Ambiguous writes
remain retained until reconciliation; bounded retries require observed absence.
Superseded cleanup uses qualified ownership, the live cursor and the same source
and history policy, and reconciles its comment and close operations separately.

New report commits carry `TauCeti-Progress-Input`, a digest of their source,
history policy, documentation pin, window, paths and roadmap layer inputs.
Artifact filenames do not affect the digest. Before writing uncommitted files,
the existing Git branch configuration records the same input digest; retries
cannot reinterpret those bytes under another policy. Both retained local and
remote commits must match the digest and the actual generated-file window.
Old unbound GitHub reports remain recoverable only under the empty history
policy. Unknown or mismatched native/nonempty-policy reports require recovery
and keep their prepared prose. The apply CLI restores the verified saved policy
snapshot for preparation. Commit metadata describes preparation inputs, not
approval authority or mathematical truth; merge policy remains independent.

The command-line entry points still use the legacy bootstrap and reject native
project configurations. Legacy apply also refuses qualified saved plans before any
legacy GitHub operation, including direct library calls, superseded-report cleanup,
or a lost `--conf`; an explicit null source binding also refuses factual collection;
prepared prose remains available for the explicitly bound native publisher.
Trusted runtime binding, announcement/workflow integration and Worker callers
must be completed before enabling that bootstrap. Worker integration must compare the source
binding in a facts result with the plan before its Progress pin is upgraded. The explicit reader interfaces prepare that
integration without routing current operations to a GitHub mirror.
