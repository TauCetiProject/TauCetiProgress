# Workflows to install in TauCetiRoadmap

These two files belong in `TauCetiRoadmap/.github/workflows/`, not here. They live in this repository
so they are reviewed alongside the gate they call, and so the pinning discipline is visible in one
place.

`/.github/` in TauCetiRoadmap is owned by `@TauCetiProject/humans`, so installing them is a
deliberate human act.

## Installing

1. Commit the two workflow files into `TauCetiRoadmap/.github/workflows/`.
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

Anyone. There is no author allowlist, and pull requests opened from forks are accepted.

What makes that safe is the shape of the diff rather than the identity behind it. A report may touch
exactly one roadmap's `STATUS.md` and `PROGRESS.md` and nothing else; the log must grow only at its
end, byte for byte; the window must continue from the area's current cursor and end at a commit
actually reachable from TauCeti's `docgen` branch; and the `build` check must have succeeded on the
exact head being merged. No pull-request content is ever checked out or executed, and no write token
exists until every check has passed.

That last condition on the window is what keeps an open door bounded rather than merely revertible.
Cursor continuity pins where a report starts, but its end was otherwise free, so a chain of reports
could have walked the cursor to arbitrary values, burning windows that could never afterwards be
reported and announcing each step to Zulip. Tying the end to published history means a bogus report
costs exactly what a real one costs, and is reverted the same way.

The residual risk is accepted and stated plainly: someone may land prose that is wrong, or replace a
`STATUS.md` with junk. `STATUS.md` is a snapshot the next run rewrites wholesale, and `PROGRESS.md`
only ever grows, so nothing is destroyed and `git revert` undoes it. These files are declared
machine-owned and their prose is not security-validated; see the repository README.

A roadmap's **first** report is the one exception: it is never auto-merged. Every later report is
pinned to the cursor already on `main`, but a first report has none, so whoever files it decides
where that roadmap's history begins — and windows only move forward, so anything before that point
becomes unreportable. Checking that choice means asking whether any labelled pull request merged
before it, which is a question about the first-parent chain that the REST API cannot answer: it
offers neither first-parent traversal nor an ordering guarantee, and this history is not linear, so
ancestry checks cannot recover it. Rather than pretend to check it, a human bootstraps each roadmap
once. The generator still writes that first report; only merging it needs a person, and everything
after is unattended.

Ask for one with `--area <Roadmap>`. Automatic selection skips roadmaps that have never been
reported, so an unbootstrapped one does not generate a report every day only to have it refused.

Operators without push access to TauCetiRoadmap publish from a fork, which `apply` sets up
automatically. Nothing has to be configured for a new contributor to start producing reports.

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
symlink, that the head is a `progress/*` branch whose name matches the window it carries, that the
window ends at a commit reachable from TauCeti's documentation branch, that `build` is green on that
exact commit, and that the merge is bound to the head that was validated.

It says nothing about *who* opened the pull request, on purpose.

It does **not** prove the prose is true. That limit is accepted deliberately; see the trust-boundary
section of the TauCetiProgress README.
