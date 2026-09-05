#!/usr/bin/env python3
"""Check deterministic suite conventions not covered by HACS or hassfest."""

from __future__ import annotations

import json
from pathlib import Path


def fail(message: str) -> None:
    """Print a policy failure in the GitHub Actions error format."""
    print(f"ERROR: {message}")


def main() -> int:
    """Check the deterministic public-repository conventions."""
    root = Path.cwd()
    suite = json.loads((root / ".github" / "suite.json").read_text())
    domain = suite["domain"]
    manifest_path = root / "custom_components" / domain / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    failures = 0

    if suite.get("kind") != "integration":
        fail("suite.json kind must be integration")
        failures += 1
    if manifest.get("domain") != domain:
        fail(f"manifest domain must be {domain!r}")
        failures += 1
    if not (root / "custom_components" / domain).is_dir():
        fail(f"missing custom_components/{domain}/")
        failures += 1

    repo = root.name
    expected_docs = f"https://github.com/ha-parcel-integrations/{repo}"
    if manifest.get("documentation") != expected_docs:
        fail("manifest documentation URL is not the canonical repository URL")
        failures += 1
    if manifest.get("issue_tracker") != f"{expected_docs}/issues":
        fail("manifest issue_tracker URL is not the canonical repository issue URL")
        failures += 1

    claude = (root / "CLAUDE.md").read_text()
    conventions = "https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md"
    if conventions not in claude:
        fail("CLAUDE.md must point to the shared conventions")
        failures += 1
    if suite["research_api_path"] not in claude:
        fail("CLAUDE.md must contain its private research API pointer")
        failures += 1

    public_api_notes = root / "docs" / "api"
    if public_api_notes.exists():
        fail("docs/api/ must not be tracked in a public integration repository")
        failures += 1

    if not failures:
        print("Policy checks passed.")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
