const fs = require("fs");

const STABLE_VERSION = /^(\d+)\.(\d+)\.(\d+)$/;
const PRERELEASE_VERSION = /^(\d+)\.(\d+)\.(\d+)b([1-9]\d*)$/;
const RELEASE_SUBJECT = /^(feat|fix)(!)?(?:\([^)]+\))?: (.+)$/;

function parseStable(version) {
  const match = STABLE_VERSION.exec(version);
  if (!match) throw new Error(`Expected a stable X.Y.Z tag, got ${version}`);
  return match.slice(1).map(Number);
}

function bumpedVersion(version, changes) {
  const [major, minor, patch] = parseStable(version);
  if (changes.some((change) => change.breaking)) return `${major + 1}.0.0`;
  if (changes.some((change) => change.type === "feat")) return `${major}.${minor + 1}.0`;
  return `${major}.${minor}.${patch + 1}`;
}

function releaseNotes(changes, helpWanted, repo) {
  const sections = [
    ["New features", changes.filter((change) => change.type === "feat")],
    ["Bug fixes", changes.filter((change) => change.type === "fix")],
  ];
  const lines = [];
  for (const [heading, entries] of sections) {
    if (!entries.length) continue;
    lines.push(`## ${heading}`, ...entries.map((entry) => `- ${entry.description}`), "");
  }
  lines.push("## Other improvements", "- This release includes reliability and maintenance improvements.", "");
  lines.push("## Credits", "- Thanks to everyone who tested and reported issues.", "");
  if (helpWanted) {
    const issueUrl = `https://github.com/ha-parcel-integrations/${repo}/issues?q=is%3Aissue+is%3Aopen+label%3A%22help+wanted%22`;
    lines.push(`🙋 [${helpWanted} open question${helpWanted === 1 ? "" : "s"} need a real parcel to answer](${issueUrl})`, "");
  }
  lines.push("---", "", "📦 [See every supported carrier](https://ha-parcel-integrations.github.io/carriers/) — new ones land regularly.", "💛 [Support the project](https://ha-parcel-integrations.github.io/sponsor/)");
  return lines.join("\n");
}

async function latestStableRelease(github, context) {
  const { data: releases } = await github.rest.repos.listReleases({
    owner: context.repo.owner,
    repo: context.repo.repo,
    per_page: 100,
  });
  const release = releases.find((item) => !item.prerelease && STABLE_VERSION.test(item.tag_name));
  if (!release) throw new Error("No stable GitHub release was found to use as the release base.");
  return release.tag_name;
}

async function changesSinceRelease(github, context, tag, head) {
  const { data } = await github.rest.repos.compareCommits({
    owner: context.repo.owner,
    repo: context.repo.repo,
    base: tag,
    head,
  });
  if (data.total_commits > data.commits.length) {
    throw new Error("More than 250 commits since the last release; split the release before continuing.");
  }
  return data.commits.flatMap((commit) => {
    const subject = commit.commit.message.split("\n", 1)[0];
    const match = RELEASE_SUBJECT.exec(subject);
    if (!match) return [];
    return [{ type: match[1], breaking: Boolean(match[2]), description: match[3] }];
  });
}

function writeManifestVersion(version) {
  const path = process.env.MANIFEST_PATH;
  const original = fs.readFileSync(path, "utf8");
  const updated = original.replace(/"version":\s*"[^"]+"/, `"version": "${version}"`);
  if (updated === original) throw new Error(`Could not find version in ${path}`);
  fs.writeFileSync(path, updated);
}

module.exports = async ({ github, context, core }) => {
  const tag = await latestStableRelease(github, context);
  const head = process.env.HEAD_SHA || context.sha;
  const changes = await changesSinceRelease(github, context, tag, head);
  const requested = process.env.RELEASE_VERSION;
  const candidate = changes.length ? bumpedVersion(tag, changes) : null;

  if (requested) {
    if (!PRERELEASE_VERSION.test(requested)) {
      throw new Error("Pre-release version must use X.Y.ZbN, for example 2.9.0b1.");
    }
    if (!candidate) throw new Error("A pre-release needs at least one feat: or fix: commit since the stable release.");
    if (!requested.startsWith(`${candidate}b`)) {
      throw new Error(`Pre-release ${requested} must be based on the calculated next version ${candidate}.`);
    }
  } else if (!candidate) {
    core.notice("No feat: or fix: commits since the last stable release; no release PR is needed.");
    core.setOutput("has_release", "false");
    return;
  }

  const version = requested || candidate;
  const { data: issues } = await github.rest.issues.listForRepo({
    owner: context.repo.owner,
    repo: context.repo.repo,
    state: "open",
    labels: "help wanted",
    per_page: 100,
  });
  writeManifestVersion(version);
  core.setOutput("has_release", "true");
  core.setOutput("version", version);
  core.setOutput("notes", `${releaseNotes(changes, issues.length, context.repo.repo)}\n\n<!-- release-manager:version=${version} -->`);
};
