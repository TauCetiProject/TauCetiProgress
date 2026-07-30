# Workflows to install in TauCetiRoadmap

These two files belong in `TauCetiRoadmap/.github/workflows/`, not here. They live in this repository
so they are reviewed alongside the gate they call, and so the pinning discipline is visible in one
place.

`/.github/` in TauCetiRoadmap is owned by `@TauCetiProject/humans`, so installing them is a
deliberate human act.

## Installing

1. Commit the two files into `TauCetiRoadmap/.github/workflows/`.
2. Replace every `REPLACE_WITH_FULL_SHA` with the full 40-character SHA of the TauCetiProgress commit
   you are pinning. Each file uses it **twice** — once in `uses:` and once in `progress_ref:` — and
   both must be that same SHA. `uses:` selects the workflow definition; `progress_ref` selects the
   validator code checked out inside it. A mismatch would validate with different rules than the ones
   reviewed.
3. Add the repository secrets `ZULIP_EMAIL`, `ZULIP_API_KEY`, and optionally `ZULIP_SITE`.
   (`APP_ID` / `APP_PRIVATE_KEY` are already present, used by the existing `auto-merge.yml`.)
4. Subscribe the Zulip bot to the **Tau Ceti** channel.
5. Add the machine-owned declaration to the repository `README.md` — see `readme-snippet.md`.

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

## What the gate actually guarantees

It proves the *shape* of an update: which paths changed, that both generated files are present, that
the window continues the log with no gap, that `PROGRESS.md` grew only at the end, that no file is a
symlink, that the author is an allowlisted numeric user id pushing a `progress/*` branch in this
repository, that `build` is green on that exact commit, and that the merge is bound to the head that
was validated.

It does **not** prove the prose is true. That limit is accepted deliberately; see the trust-boundary
section of the TauCetiProgress README.
