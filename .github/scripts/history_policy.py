#!/usr/bin/env python3
"""Prepare a history policy from a protected, immutable workflow checkout."""

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tauceti_tools.ci_candidate import read_json
from tauceti_tools.forge import GITHUB, RepositoryRef

from progress.provenance import MAX_MAP_BYTES, MigrationMap


def source(repository, revision, relative):
    if not repository:
        if revision or relative != "progress-history.json":
            raise ValueError("history policy repository and immutable ref must be configured together")
        return
    RepositoryRef(GITHUB, repository)
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None or not int(revision, 16):
        raise ValueError("history policy ref must be an immutable 40-hex commit")
    path = pathlib.PurePosixPath(relative)
    if (
        not relative
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in relative
        or any(c in relative for c in "\0\r\n")
    ):
        raise ValueError("history policy path must stay inside its trusted checkout")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", default="")
    parser.add_argument("--ref", default="")
    parser.add_argument("--path", default="progress-history.json")
    parser.add_argument("--root", default=".tauceti-history-policy")
    parser.add_argument("--validate-source", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--github-env")
    args = parser.parse_args(argv)
    try:
        source(args.repository, args.ref, args.path)
        if args.validate_source:
            return 0
        policy = MigrationMap()
        if args.repository:
            current = pathlib.Path(args.root).absolute()
            if current.is_symlink() or not current.is_dir():
                raise ValueError("history policy checkout is unavailable or symlinked")
            for part in pathlib.PurePosixPath(args.path).parts:
                current = current / part
                if current.is_symlink():
                    raise ValueError("history policy path must not contain symlinks")
            if not current.is_file():
                raise ValueError("history policy must be a regular file")
            with current.open("rb") as stream:
                raw = stream.read(MAX_MAP_BYTES + 1)
            policy = MigrationMap.parse(read_json(raw, max_bytes=MAX_MAP_BYTES))
        if not args.output:
            raise ValueError("history policy needs an output snapshot")
        output = pathlib.Path(args.output).absolute()
        if any(c in str(output) for c in "\r\n"):
            raise ValueError("invalid policy output path")
        output.write_bytes(policy.canonical_bytes())
        if args.github_env:
            with open(args.github_env, "a", encoding="utf-8") as stream:
                stream.write(f"TAUCETI_PROGRESS_HISTORY_MAP={output}\n")
        print(f"history_migration_sha256={policy.digest}")
        return 0
    except (OSError, ValueError) as error:
        print(f"history policy: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
