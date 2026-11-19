"""Bind retained report commits to their source and factual inputs.

This is commit/branch metadata in the existing report checkout. It neither owns
an outbox nor chooses credentials, storage locations or publication authority.
"""

import hashlib
import json
import re

from tauceti_tools.forge import GITHUB, RepositoryRef

from . import gh
from .native_source import history_options_from_record
from .provenance import MigrationMap, ProvenanceError, policy_digest

TRAILER = "TauCeti-Progress-Input: "
_TRAILER = re.compile(r"^TauCeti-Progress-Input: ([0-9a-f]{64})$", re.M)


class InputMismatch(ValueError):
    pass


def record(plan):
    """Canonical model-input identity, independent of artifact file locations."""
    source = plan.get("source")
    if "source" in plan:
        try:
            history_options_from_record(source)
        except (ValueError, TypeError):
            raise InputMismatch("invalid retained report source binding") from None
    else:
        source = {
            role: RepositoryRef(GITHUB, name).key
            for role, name in (("content", gh.CODE_REPO), ("roadmap", gh.ROADMAP_REPO))
        }
    try:
        policy = policy_digest(plan.get("history_migration_sha256", MigrationMap().digest))
    except ProvenanceError as error:
        raise InputMismatch(str(error)) from None
    documentation = plan.get("documentation")
    if documentation is not None:
        if not isinstance(documentation, dict):
            raise InputMismatch("invalid retained report documentation binding")
        documentation = {key: value for key, value in documentation.items() if key != "bundle_file"}
    return {
        "schema": "tauceti-progress.publication-input/v1",
        "source": source,
        "history_migration_sha256": policy,
        "documentation": documentation,
        **{key: plan.get(key) for key in (
            "roadmap", "from_sha", "to_sha", "status_path", "progress_path", "readme_sha", "layers", "sub_roadmaps",
        )},
        "prs": sorted(plan["prs"]),
    }


def digest(plan):
    return hashlib.sha256(json.dumps(record(plan), sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def legacy_empty(plan):
    """Old unbound GitHub reports remain recoverable only under the empty policy."""
    return "source" not in plan and record(plan)["history_migration_sha256"] == MigrationMap().digest


def require_message(plan, message):
    matches = _TRAILER.findall(message)
    # A malformed lookalike is not a legacy commit with an absent binding.
    mentions = [line for line in message.splitlines() if line.lstrip().casefold().startswith(TRAILER.casefold().rstrip())]
    if not mentions and legacy_empty(plan):
        return
    if len(mentions) != 1 or len(matches) != 1 or matches[0] != digest(plan):
        raise InputMismatch("retained report source or factual policy differs; preserve its prepared prose")
