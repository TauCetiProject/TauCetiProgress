# Workflows to install in TauCetiRoadmap

These two files belong in `TauCetiRoadmap/.github/workflows/`, not here. They live in this repository
so they are reviewed alongside the gate they call, and so the pinning discipline is visible in one
place.

`/.github/` in TauCetiRoadmap is owned by `@TauCetiProject/humans`, so installing them is a
deliberate human act.

## Installing

1. Commit the two workflow files into `TauCetiRoadmap/.github/workflows/`, and
   `progress-publishers.txt` into `TauCetiRoadmap/.github/`.
2. Replace every `REPLACE_WITH_FULL_SHA` with the full 40-character SHA of the TauCetiProgress commit
   you are pinning. Each file uses it **twice** — once in `uses:` and once in `progress_ref:` — and
   both must be that same SHA. `uses:` selects the workflow definition; `progress_ref` selects the
   validator code checked out inside it. A mismatch would validate with different rules than the ones
   reviewed.
3. Add the repository secrets `ZULIP_EMAIL`, `ZULIP_API_KEY`, and optionally `ZULIP_SITE`.
   (`APP_ID` / `APP_PRIVATE_KEY` are already present, used by the existing `auto-merge.yml`.)
4. Subscribe the Zulip bot to the **Tau Ceti** channel.
5. Add the machine-owned declaration to the repository `README.md` — see `readme-snippet.md`.

## Who may publish

`.github/progress-publishers.txt` in TauCetiRoadmap lists the numeric user ids whose progress pull
requests may merge unattended. It is a file rather than a workflow input for two reasons: adding a
publisher is then an ordinary reviewed pull request instead of an edit to a workflow, and an
operator's own tooling can read the same list to decide, before spending anything, whether a round
could ever land.

The merge check reads it at the pull request's **base** commit, never its head, so a pull request
cannot list its own author. `/.github/` belongs to `@TauCetiProject/humans` in CODEOWNERS, so an
addition takes a core-team review.

A listed account also needs push access to TauCetiRoadmap, because the report branch is pushed to
this repository directly and the merge check refuses fork heads. The two conditions are independent,
and `tauceti-progress due` checks both.

Listing someone does grant privilege they would not otherwise have: everyone except an organization
admin currently needs a second reviewer to land anything here, and a publisher can land two
machine-owned markdown files alone. The blast radius is bounded by the merge check to exactly those
files, in one roadmap directory, with the log append-only, but the prose itself is not validated.
Treat a listing as the same order of trust as roadmap review.

## Keeping versions in step

Three places run TauCetiProgress code, and they must be the same commit:

| Where | How it is pinned |
| --- | --- |
| `progress-merge.yml` / `progress-announce.yml` | `uses:` SHA and `progress_ref:` |
| The worker's `apply`/`plan` invocation | `uvx --from git+…@<sha>` |
| The validator checked out inside the reusable workflows | `progress_ref:` |

If the worker runs a newer version than the gate, it can emit headers the gate does not recognise and
every report wedges. Bump them together, and note that `apply` records the version it ran in each
pull request body so a mismatch is diagnosable after the fact.

## A standing invariant: `main` must never rewind

The landing step is a compare-and-swap: the commit is built on the validated `main` SHA and the ref
is updated with `force=false`, which GitHub rejects unless the update is a fast-forward. That is an
exact swap for every ordinary case, with one residual the REST API cannot express — there is no
"expected old SHA" parameter, only "must be a fast-forward". So if another bypass actor *rewound*
`main` to an ancestor of the validated SHA, this commit would still be a fast-forward of the rewound
tip and would be accepted, even though `main` was no longer where it was validated.

Keep `main` non-rewindable. The `main` ruleset already carries `non_fast_forward`, which forbids
exactly this for everyone the ruleset applies to; the residual is limited to the bypass actors
(organisation admins and the sync App). Treat "nobody force-pushes `main`" as a rule of the
repository rather than something the workflow can enforce.

## What the gate actually guarantees

It proves the *shape* of an update: which paths changed, that both generated files are present, that
the window continues the log with no gap, that `PROGRESS.md` grew only at the end, that no file is a
symlink, that the author is an allowlisted numeric user id pushing a `progress/*` branch in this
repository, that `build` is green on that exact commit, and that the merge is bound to the head that
was validated.

It does **not** prove the prose is true. That limit is accepted deliberately; see the trust-boundary
section of the TauCetiProgress README.
