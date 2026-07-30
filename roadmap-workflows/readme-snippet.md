Add this to `TauCetiRoadmap/README.md`, so no reader mistakes a generated file for reviewed roadmap
content.

---

## Generated status files

Each roadmap directory may carry two files that are **written by machine, not by hand**:

- `STATUS.md` — a snapshot of where that roadmap stands, rewritten whole on each update and headed by
  the TauCeti commit it describes. It is updated asynchronously from the work it reports, so it is
  never authoritative about the current tip.
- `PROGRESS.md` — an append-only log, one section per window of merged pull requests. New sections are
  announced in the **Tau Ceti > Progress logs** Zulip topic.

Both are produced by [TauCetiProgress](https://github.com/TauCetiProject/TauCetiProgress) and merge
without human review, under a gate that checks their structure. **Their prose is not
security-validated**: the gate proves which paths changed and that the log only grew at the end, but
it cannot prove that the summary is accurate. Read them as a machine's account of the work, and treat
the roadmap `README.md` beside them — which humans own and review — as the authority on what the
roadmap actually asks for.
