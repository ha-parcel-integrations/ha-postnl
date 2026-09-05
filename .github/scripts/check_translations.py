#!/usr/bin/env python3
"""Ensure every translation has the same JSON key structure as English."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def leaf_paths(value: object, prefix: str = "") -> set[str]:
    """Return every leaf path in a JSON value."""
    if isinstance(value, dict):
        paths: set[str] = set()
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else key
            paths.update(leaf_paths(child, child_prefix))
        return paths
    return {prefix}


def main() -> int:
    """Validate all translation files against the English key structure."""
    translations = Path(sys.argv[1])
    english_path = translations / "en.json"
    if not english_path.is_file():
        print(f"ERROR: missing source translation: {english_path}")
        return 1

    english = leaf_paths(json.loads(english_path.read_text()))
    failures = 0
    for path in sorted(translations.glob("*.json")):
        if path == english_path:
            continue
        actual = leaf_paths(json.loads(path.read_text()))
        missing = sorted(english - actual)
        extra = sorted(actual - english)
        if missing or extra:
            failures += 1
            print(f"ERROR: {path}")
            if missing:
                print("  missing: " + ", ".join(missing))
            if extra:
                print("  extra: " + ", ".join(extra))

    if not failures:
        print("Translation structures match en.json.")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
