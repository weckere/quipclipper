#!/usr/bin/env bash
# Cut a quipclipper release: scripts/release.sh X.Y.Z
#
# On a clean dev that matches origin/dev, this bumps the version in both
# pyproject.toml files, turns CHANGELOG.md's Unreleased section into
# "## [X.Y.Z] - <today>", commits "release: vX.Y.Z", tags vX.Y.Z, and pushes
# dev, main (a fast-forward to the release commit) and the tag in one atomic
# push. The tag starts .github/workflows/release.yml: tests, GHCR images and
# the GitHub Release.
#
#   --dry-run   do every check and show the changes, then put the files back
#               without committing, tagging or pushing.
set -euo pipefail

die() { echo "release: $*" >&2; exit 1; }

dry_run=0
args=()
for a in "$@"; do
  case "$a" in
    --dry-run) dry_run=1 ;;
    -h|--help) sed -n '2,13s/^# \{0,1\}//p' "$0"; exit 0 ;;
    *) args+=("$a") ;;
  esac
done
[ "${#args[@]}" -eq 1 ] || die "usage: scripts/release.sh [--dry-run] X.Y.Z"
version=${args[0]#v}
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "not a version: ${args[0]} (want X.Y.Z)"
tag="v$version"

cd "$(git rev-parse --show-toplevel)"

# --- checks -----------------------------------------------------------------
branch=$(git symbolic-ref --quiet --short HEAD || true)
[ "$branch" = dev ] || die "releases are cut from dev (you are on ${branch:-a detached HEAD})"
[ -z "$(git status --porcelain)" ] || die "the working tree is not clean"

git fetch --quiet --tags origin dev main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/dev)" ] \
  || die "dev is not the same as origin/dev (pull or push first)"
git merge-base --is-ancestor origin/main HEAD \
  || die "origin/main is not an ancestor of dev, so main can't fast-forward"
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null \
  || git ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null; then
  die "$tag already exists"
fi

current=$(sed -n 's/^version = "\([^"]*\)".*/\1/p' pyproject.toml | head -n1)
web_current=$(sed -n 's/^version = "\([^"]*\)".*/\1/p' web/backend/pyproject.toml | head -n1)
[ "$current" = "$web_current" ] || die "pyproject.toml says $current, web/backend says $web_current"
newest=$(printf '%s\n%s\n' "$current" "$version" | sort -t. -k1,1n -k2,2n -k3,3n | tail -n1)
[ "$version" != "$current" ] && [ "$newest" = "$version" ] \
  || die "$version is not newer than the current $current"

grep -q '^## \[Unreleased\]' CHANGELOG.md || die "CHANGELOG.md has no ## [Unreleased] section"
unreleased=$(awk '/^## \[Unreleased\]/ { on = 1; next } on && /^## / { exit } on' CHANGELOG.md \
  | grep -v '^\[' | grep -c '[^[:space:]]' || true)
[ "$unreleased" -gt 0 ] || die "CHANGELOG.md's Unreleased section is empty: nothing to release"

previous_tag=$(git describe --tags --abbrev=0 --match 'v[0-9]*' 2>/dev/null || true)
today=$(date +%Y-%m-%d)

# --- changes ----------------------------------------------------------------
sed -i.bak -E "1,/^version = \"[^\"]*\"/s/^version = \"[^\"]*\"/version = \"$version\"/" \
  pyproject.toml web/backend/pyproject.toml
rm -f pyproject.toml.bak web/backend/pyproject.toml.bak

VERSION="$version" TODAY="$today" PREVIOUS="$previous_tag" python3 - <<'PY'
import os, re
from pathlib import Path

v, today, prev = os.environ["VERSION"], os.environ["TODAY"], os.environ["PREVIOUS"]
repo = "https://github.com/weckere/quipclipper"
p = Path("CHANGELOG.md")
text = p.read_text()
text = text.replace("## [Unreleased]\n", f"## [Unreleased]\n\n## [{v}] - {today}\n", 1)
links = [f"[Unreleased]: {repo}/compare/v{v}...dev",
         f"[{v}]: {repo}/compare/{prev}...v{v}" if prev else f"[{v}]: {repo}/releases/tag/v{v}"]
text = re.sub(r"^\[Unreleased\]: .*$", "\n".join(links), text, count=1, flags=re.M)
p.write_text(text)
PY

for f in pyproject.toml web/backend/pyproject.toml; do
  grep -q "^version = \"$version\"" "$f" || die "failed to bump $f"
done
grep -q "^## \[$version\] - $today" CHANGELOG.md || die "failed to date CHANGELOG.md"

git --no-pager diff --stat
if [ "$dry_run" = 1 ]; then
  git --no-pager diff
  git checkout -- pyproject.toml web/backend/pyproject.toml CHANGELOG.md
  echo "release: dry run of $tag ok (from ${previous_tag:-the start}); nothing committed or pushed"
  exit 0
fi

# --- commit, tag, push ------------------------------------------------------
git commit --quiet -m "release: $tag" -- pyproject.toml web/backend/pyproject.toml CHANGELOG.md
git tag -a "$tag" -m "quipclipper $version"
git push --atomic origin HEAD:refs/heads/dev HEAD:refs/heads/main "refs/tags/$tag"

echo "release: $tag pushed (dev, main, tag)."
echo "  Release workflow: https://github.com/weckere/quipclipper/actions/workflows/release.yml"
echo "  A local main branch, if you keep one, is now behind: git fetch origin main:main"
