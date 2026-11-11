"""The file behind `plan --label-cache`: each area's labelled pull requests, kept between runs.

One file serves one content repository on one forge, named by `binding`, so a GitHub file is never
read for a Forgejo repository or the reverse. The forge-specific half is a reader with three calls:

* `full(areas)` -- `{area: {number: sha_or_None}}`, the complete labelled histories;
* `start(areas)` -- a cursor marking now for `areas`, taken before this run reads anything, so
  whatever changes during the run is still after it;
* `changes(cursor, areas)` -- `{number: (areas_labelled, sha_or_None)}`, every pull request whose
  membership may have changed since `cursor`, with its labels as they now are.

Neither forge's incremental read sees a label added to or removed from a pull request that has
not otherwise changed, so the file is read in full again once it is `FULL_HOURS` old, and a planner
rereads its chosen area in full (`refresh`) before it plans the report.
"""

import datetime
import json
from pathlib import Path

VERSION = 2
FULL_HOURS = 24.0


def _load(path, binding):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if (data["version"] != VERSION or data["binding"] != binding
            or not isinstance(data["areas"], dict)
            or not all(isinstance(prs, dict) for prs in data["areas"].values())):
        raise ValueError("stale or foreign label cache")
    return data


def _store(path, data):
    from .docs import store

    store(Path(path), (json.dumps(data, sort_keys=True) + "\n").encode("utf-8"))


def read(path, areas, reader, binding, now=None):
    """The labelled histories of `areas`, from the file at `path` and what `reader` says changed.

    A file that is unreadable, for another binding or from another version is discarded, and one
    that cannot be written costs the next run a full read. A reader raises ValueError to ask for a
    full read.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    try:
        data = _load(path, binding)
        age = (now - datetime.datetime.fromisoformat(data["full_at"])).total_seconds()
        if not 0 <= age < FULL_HOURS * 3600:
            raise ValueError("label cache is due a full read")
        cursor = reader.start(sorted(set(data["areas"]) | set(areas)))
        changed = reader.changes(data["cursor"], sorted(data["areas"]))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        data = {"version": VERSION, "binding": binding, "full_at": stamp, "areas": {}}
        cursor, changed = reader.start(sorted(set(areas))), {}
    cached = data["areas"]
    for number, (labelled, sha) in changed.items():
        for area, prs in cached.items():
            if area in labelled:
                prs[str(number)] = sha
            else:
                prs.pop(str(number), None)
    missing = [area for area in dict.fromkeys(areas) if area not in cached]
    for area, prs in reader.full(missing).items():
        cached[area] = {str(number): sha for number, sha in prs.items()}
    data.update(cursor=cursor, synced_at=stamp)
    _store(path, data)
    return {area: {int(number): sha for number, sha in cached[area].items()} for area in areas}


def refresh(path, area, reader, binding):
    """`reader.full([area])`, filed in the file at `path` in place of the area's entry. A file that
    cannot be read is left for its next full read."""
    prs = reader.full([area])[area]
    try:
        data = _load(path, binding)
    except (OSError, ValueError, KeyError, TypeError):
        return prs
    data["areas"][area] = {str(number): sha for number, sha in prs.items()}
    _store(path, data)
    return prs
