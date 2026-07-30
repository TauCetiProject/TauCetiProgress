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
[TauCetiWorker](https://github.com/kim-em/TauCetiWorker) drives it.

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
tauceti-progress announce --section FILE  post a new section to Zulip (idempotent)
```

`due` is the only one that runs often; it exits 75 ("no progress") when nothing is due, matching
the worker's convention. `plan` runs at most once a day.

## The window cursor is a SHA

A window is the half-open commit range `(from_sha, to_sha]` on TauCeti's `main`, where `from_sha`
is the `to_sha` of the previous `PROGRESS.md` section. PR numbers come from the squash-merge commit
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
message is size-capped, and it is idempotent on a stable per-window id -- but it is a second sink and
the threat model has to say so.

## Licence

Apache-2.0.
