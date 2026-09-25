# TauCetiProgress

Progress reporting for [Tau Ceti](https://github.com/TauCetiProject/TauCeti): what has actually
been achieved on each roadmap, written for a human to read in a minute.

Each roadmap directory in
[TauCetiRoadmap](https://github.com/TauCetiProject/TauCetiRoadmap) carries two generated files:

- **`STATUS.md`** — a snapshot, rewritten whole on each update. It says which parts of the roadmap
  are done and sketches the frontier, headed by the commit it describes.
- **`PROGRESS.md`** — an append-only log. Each section covers one window of merged PRs as a few
  holistic paragraphs, emphasising named theorems rather than listing every PR.

New `PROGRESS.md` sections are announced in the **Tau Ceti > Progress logs** Zulip topic.

## Why this repo exists

The rubrics-and-machinery split of
[TauCetiReview](https://github.com/TauCetiProject/TauCetiReview), applied to reporting: the
prompts and the tooling live here, the output lands in TauCetiRoadmap, and
[TauCetiWorker](https://github.com/TauCetiProject/TauCetiWorker) drives it.

The design rule is that **a model only ever writes prose**. Every decision — whether an update is
due, which roadmap it covers, which PRs are in the window, and what mathematics actually landed —
is made by tested Python before any model starts, and the git and pull-request work afterwards is
done by tested Python too.

## The commands

```
tauceti-progress due                      is an update due? (one API call, no clone)
tauceti-progress plan   --roadmap-dir DIR pick the roadmap and the PR window
tauceti-progress facts  --plan FILE       what declarations actually landed in the window
tauceti-progress apply  --plan FILE ...   write the files, open the PR (resumable)
tauceti-progress sweep  --area AREA ...   retire the reports a landing just orphaned
tauceti-progress announce --section FILE  post a new section to Zulip (idempotent)
```

`due` is the only one that runs often; it exits 75 ("no progress") when nothing is due, matching
the worker's convention. `plan` runs at most once a day. `sweep` is not run by the worker at all:
the merge workflow calls it after a landing, for the reason in the next section.

## An area may hold several open reports, and only one can win

Two operators, or one operator across two rounds, can have reports open for the same area at once.
They cannot both land: the gate requires a byte-exact append at the cursor in `PROGRESS.md`, so the
moment one lands and the cursor moves, every other open report for that area is unmergeable for
good.

Nothing used to notice, and they accumulated — one unmergeable report per area per round, never
shed. What retires them is `sweep`, called by the merge workflow *after* the compare-and-swap has
already chosen a winner.

After, specifically, because the alternative does not work. Retiring a report on the grounds that
some other open report covers more of the window means acting on a prediction, and a prediction can
be wrong in three ways that all cost real work: the favoured report may fail its build, leaving
nothing landed and the retired one closed-unmerged, which `apply` treats as permanently refused; the
close may race the merge workflow, which reads a pull request's state when it collects and trusts
that snapshot until it writes; and "covers more" has to be read from a pull request body, which
anyone can edit. Waiting for the ref update removes all three, because there is then nothing left to
predict — only a fact to observe.

The fact is read *positively*, from committed history: a report is retired only when `PROGRESS.md`
shows its starting cursor already appended at and moved past. The near-miss is to retire on
disagreement with the current cursor instead, which sounds equivalent and is not — a contents read
can be stale, and a report starting *ahead* of a stale answer disagrees with it exactly as loudly as
a spent one. Reading less history can only shrink the evidence, so a stale answer retires fewer
reports rather than a live one.

That holds for full SHAs, not for the seven characters a branch name carries: a report starting at a
commit the stale read has not seen can share its prefix with a spent cursor. The branch name only
nominates candidates; a report is retired when the full `from_sha` of the section its own head
appends is a spent cursor, and kept whenever that cannot be read. For the same reason, "is there a
same-named roadmap under the other parent" is answered only by a 404; a lookup that fails retires
nothing, since reports cannot be attributed to one roadmap or the other when both exist.

Only branches matching the gate's own grammar, targeting the branch a report must target, are ever
touched. Anything else is somebody's ordinary pull request that happens to begin with `progress/`,
and failing an automated gate is not a reason to close a human's work.

## The window cursor is a SHA, on the docs-tracking branch

A window is the half-open commit range `(from_sha, to_sha]` on TauCeti's **`docgen`** branch, where
`from_sha` is the `to_sha` of the previous `PROGRESS.md` section.

`docgen` nominates the most recent commit on `main` whose API documentation has been published, and
the window ends at **the commit the published documentation actually reports** — read from the site
itself, since the deploy is independent and the branch can sit ahead of it. Ending at the branch tip
instead would record a cursor covering work the report never described, and because the next window
starts after that cursor, the work in between would never be reported at all.

The cost is latency: a report describes the project as of the last published docs build rather than
the tip. That is the right trade for a document whose whole purpose is to be read, and the header
records the exact commit, so nothing is misdated.

Links are not computed by this project at all, and neither are declaration names. Both are read from
doc-gen4's own published output — the declaration index and each module page, which carry the exact
name, kind, source file and line range — so a link resolves because it was read from the page it
points at. `git blame` over those line ranges is what decides whether a declaration belongs to the
window.

There is deliberately no Lean parser here. Qualifying a name correctly means resolving `namespace`
against `section`, `end`, `open ... in` and `_root_`, and many real declarations (projections,
constructors, `deriving` output) are never written in the source at all. An approximation gets most
names right, which is the worst outcome available: the wrong ones are indistinguishable from the
right ones, and a link built from a wrong name is a plausible dead link. PR numbers come from the squash-merge commit
subjects in that range and are attributed by their `roadmap/<Area>` label.

Wall-clock time is used only for display. A cursor made of timestamps would be wrong: a worker
clock running fast advances it past PRs whose merge times then fall *before* the stored cursor, and
those PRs are never reported at all.

## Trust boundary

`STATUS.md` and `PROGRESS.md` are **machine-owned, and their prose is not security-validated**.

The merge gate proves the *shape* of a generated update — its paths, its cursor, that it is a
byte-exact append, and that the head being merged is the head that was validated. It cannot prove
that the prose is true. Anyone may open a Tau Ceti PR whose description contains prompt-injection
text; once that PR merges legitimately, its description reaches the writing model.

The mitigations reduce the risk and are not claimed to remove it: the model is grounded in
mechanically-extracted declaration names rather than author prose, PR bodies are delimited and
size-capped, reserved `tauceti-*:v1` markers are rejected in model output, and Zulip mentions are
defused. Read these two files as a machine's summary, not as reviewed roadmap content.

**The blast radius is two markdown files AND a Zulip message.** Every merged section is posted to
**Tau Ceti > Progress logs** automatically, so accepted prose reaches an audience outside the
repository. The post is treated as data -- mentions and bare `#123` linkifiers are defused, the
message is size-capped, it links both the appended log and the current roadmap status, and it is
idempotent on a stable per-window id -- but it is a second sink and the threat model has to say so.

## Licence

Apache-2.0.
