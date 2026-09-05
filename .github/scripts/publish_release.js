const STABLE_VERSION = /^\d+\.\d+\.\d+$/;
const VERSION_IN_SUBJECT = /^Bump version to (\d+\.\d+\.\d+(?:b\d+)?)$/;

module.exports = async ({ github, context, core }) => {
  const sha = process.env.HEAD_SHA || context.sha;
  const { data: commit } = await github.rest.repos.getCommit({
    owner: context.repo.owner,
    repo: context.repo.repo,
    ref: sha,
  });
  const match = VERSION_IN_SUBJECT.exec(commit.commit.message.split("\n", 1)[0]);
  if (!match) {
    core.notice("The validated main commit is not a release bump.");
    return;
  }
  const version = match[1];
  const { data: pullRequests } = await github.rest.repos.listPullRequestsAssociatedWithCommit({
    owner: context.repo.owner,
    repo: context.repo.repo,
    commit_sha: sha,
  });
  const marker = `<!-- release-manager:version=${version} -->`;
  const releasePr = pullRequests.find((pr) => pr.merged_at && pr.body && pr.body.includes(marker));
  if (!releasePr) {
    core.notice("The version bump was not created by the release manager.");
    return;
  }
  try {
    await github.rest.git.getRef({ owner: context.repo.owner, repo: context.repo.repo, ref: `tags/${version}` });
    core.notice(`Tag ${version} already exists; nothing to publish.`);
    return;
  } catch (error) {
    if (error.status !== 404) throw error;
  }
  await github.rest.git.createRef({
    owner: context.repo.owner,
    repo: context.repo.repo,
    ref: `refs/tags/${version}`,
    sha,
  });
  const body = releasePr.body.replace(marker, "").trim();
  await github.rest.repos.createRelease({
    owner: context.repo.owner,
    repo: context.repo.repo,
    tag_name: version,
    target_commitish: sha,
    name: version,
    body,
    prerelease: !STABLE_VERSION.test(version),
  });
  core.notice(`Published ${version}.`);
};
