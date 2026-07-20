#!/usr/bin/env bash
#
# Refresh files vendored from other repos. Run by scripts/bump-version.sh before it
# commits, so a release can never ship a stale copy — and runnable by hand any time.
#
# Vendored right now: plugins/claude/bin/sharedserver, the sharedserver resolver.
# There is no way to share a file between plugins — marketplace symlinks do not cross
# repos, plugin dependencies control installation but not hook ORDER (hooks run in
# parallel), and OpenCode has no dependency mechanism at all. So it is copied, and the
# job is to make the copy verifiable rather than to pretend it is not a copy.
#
# The copy is BYTE-IDENTICAL to upstream: everything repo-specific lives in
# plugins/claude/bin/sharedserver.conf beside it. That is what lets the CI drift check
# be a plain diff.
#
# Source is sharedserver's LATEST RELEASE, not main: vendoring from a moving branch
# would ship unreleased changes. `releases/latest` excludes prereleases, so an alpha
# can never be picked up.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
die() { echo "error: $*" >&2; exit 1; }

UPSTREAM_REPO="georgeharker/sharedserver"
# Both halves of the same resolver: the Claude hook uses the shell one, the OpenCode
# plugin imports the TypeScript one. Kept behaviourally identical upstream; vendored
# byte-identical here so the drift check is a plain diff.
VENDORED_PATHS=(
  "plugins/claude/bin/sharedserver"
  "plugins/opencode/src/sharedserver-resolve.ts"
)

command -v curl >/dev/null 2>&1 || die "curl is required to sync vendored files"

# Resolve the latest release tag. Failing here is deliberate: silently skipping would
# tag a release with a stale vendored file, which is the exact thing this prevents.
tag="$(curl -sSfL "https://api.github.com/repos/${UPSTREAM_REPO}/releases/latest" 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tag_name",""))' 2>/dev/null || true)"
[ -n "$tag" ] || die "could not resolve the latest ${UPSTREAM_REPO} release (offline, or no releases yet)"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

# Contract with bump-version.sh: `VENDORED:<path>` lines on STDOUT name the files this
# script owns, so they can be staged into the release commit. Everything human-readable
# goes to stderr so it cannot pollute that list.
for path in "${VENDORED_PATHS[@]}"; do
  src="https://raw.githubusercontent.com/${UPSTREAM_REPO}/${tag}/${path}"
  curl -sSfL "$src" -o "$tmp" || die "could not fetch ${src}"
  [ -s "$tmp" ] || die "fetched an empty file from ${src}"

  dest="$ROOT/$path"
  mkdir -p "$(dirname "$dest")"
  if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
    echo "  $path already current ($tag)" >&2
  else
    cp "$tmp" "$dest"
    case "$path" in */bin/*) chmod +x "$dest" ;; esac
    echo "  $path <- ${UPSTREAM_REPO}@${tag}" >&2
  fi
  printf 'VENDORED:%s\n' "$path"
done

# Record the upstream release these copies came from, so the CI drift check and anyone
# reading the tree can tell which version they correspond to.
stamp="plugins/.sharedserver-version"
mkdir -p "$ROOT/$(dirname "$stamp")"
printf '%s\n' "$tag" > "$ROOT/$stamp"
printf 'VENDORED:%s\n' "$stamp"
