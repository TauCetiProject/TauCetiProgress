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

## The coverage header

A status snapshot may carry a second machine header beside `tauceti-status:v1`:

```text
<!--tauceti-status:v1 {"roadmap":"EllipticCurves","to_sha":"…","ts":"…"}-->
<!--tauceti-coverage:v1 {"layers":[{"id":"Layer 0","remaining":"Weil reciprocity","state":"partial"},{"id":"Layer 1","state":"done"},…],"readme_sha":"…","roadmap":"EllipticCurves","to_sha":"…"}-->
# Status: EllipticCurves
```

It is the report's verdict on each layer of the roadmap, in a form a script can read: the prose
already says which layers are done, partial or untouched, but nothing can aggregate prose across
forty roadmaps. The consumer is the TauCeti site's Progress page (`scripts/roadmap_progress.py`
in the TauCeti repository), which shows one strip per roadmap, one segment per layer.

How it is made, following the design rule above that a model only ever writes prose:

- `plan` extracts the roadmap's **layers** from its README (the `Layer` / `Lane` / `Part` /
  `Stage` headings, or `L0A`-style labels) and records them in the plan with their ids, together
  with `readme_sha`, a SHA-256 of the README's text. A README with no such headings yields no
  layers and no header.
- The writing prompt asks the model to end the status prose with a fenced ```` ```coverage ````
  block, one line per listed id: `Layer 0: partial — what remains`. The state is one of `done`,
  `partial`, `untouched`, `unassessed` (the material said nothing), and the optional note after
  the dash is one line of at most 200 characters.
- `apply` removes the block from the prose, checks that it names every listed layer exactly once
  and nothing else, and writes the header. A block that does not fit is refused on the worker; a
  body with no block gives a report with no header, exactly as before this header existed.
- The gate treats the header as part of the canonical prefix: it must sit on the line after the
  status header and nowhere else, name the same roadmap and commit, and pass the closed schema.
  It has the same standing as the prose it summarises: not security-validated, and the gate proves
  its shape, never its truth.

`readme_sha` is what binds the assessment to the specification it was made against. Layer ids
alone do not: a layer's requirements can change under an unchanged heading. The consumer refuses a
header whose hash does not match the README it read the layers from, and shows those layers as
unassessed with that reason, rather than applying an old verdict to new requirements. The gate
cannot check the hash (it never checks out the roadmap repository), which is one more reason the
header is a claim, not a certificate.

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
