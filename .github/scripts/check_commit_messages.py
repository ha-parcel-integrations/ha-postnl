#!/usr/bin/env python3
"""Enforce the suite's one-line Conventional Commit subjects."""

from __future__ import annotations

import re
import subprocess
import sys

CONVENTIONAL = re.compile(
    r"^(?:feat|fix|refactor|docs|test|ci|chore|build|perf|style|revert)(?:\([^)]+\))?!?: .+"
)
RELEASE_BUMP = re.compile(r"^Bump version to \d+\.\d+\.\d+(?:b\d+)?$")


def main() -> int:
    """Validate the subjects and permitted trailers in a commit range."""
    commit_range = sys.argv[1]
    result = subprocess.run(
        ["git", "log", "--format=%H%x1f%s%x1f%b%x1e", commit_range],
        check=True,
        text=True,
        capture_output=True,
    )
    failures = 0
    for item in result.stdout.split("\x1e"):
        if not item.strip():
            continue
        sha, subject, body = item.rstrip("\n").split("\x1f", 2)
        allowed_body = "" if not body.strip() else re.sub(
            r"^\n?Co-Authored-By: [^\n]+\n?$", "", body
        )
        if not (CONVENTIONAL.fullmatch(subject) or RELEASE_BUMP.fullmatch(subject)):
            print(f"ERROR: {sha[:7]} has a non-conventional subject: {subject}")
            failures += 1
        if allowed_body.strip():
            print(f"ERROR: {sha[:7]} has a commit body; only Co-Authored-By is allowed")
            failures += 1
    if not failures:
        print("Commit messages passed.")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
