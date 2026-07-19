#!/usr/bin/env bash
#
# Bump the whole repo's version in FULL LOCKSTEP, then commit and tag.
#
# Writes ONE version into EVERY version-bearing manifest this repo has:
#   - pyproject.toml            (version = "X.Y.Z")                  — if present
#   - Cargo.toml                ([package] version = "X.Y.Z")        — if present
#   - the Claude Code plugin    plugins/*/.claude-plugin/plugin.json ("version")
#   - the opencode plugin       plugins/opencode/package.json        ("version")
# and creates ONE release tag:
#   - vX.Y.Z                    (the unified release trigger for PyPI / npm / crate)
#
# Baseline is the highest current version among the manifests, so nothing ever
# moves backward. A repo with no nvim version manifest still takes its nvim
# release from the same vX.Y.Z tag.
#
# Usage:
#   scripts/bump-version.sh 0.4.0        # explicit version
#   scripts/bump-version.sh patch        # bump patch from the highest current
#   scripts/bump-version.sh minor        # bump minor, zero patch
#   scripts/bump-version.sh major        # bump major, zero minor+patch
#
# Options:
#   --no-tag       update + commit, skip the tag
#   --no-commit    update files only (implies --no-tag)
#   -n, --dry-run  print what would change; touch nothing
#
# Refuses a dirty tree (unless --no-commit) so the bump is its own clean commit.
# Pushing is left to you.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
die() { echo "error: $*" >&2; exit 1; }

# ---- locate manifests -------------------------------------------------------
MANIFESTS=()   # every file that gets the new version
TARGETS=()     # parallel: "toml" | "json"

PYPROJECT="$ROOT/pyproject.toml"
[ -f "$PYPROJECT" ] && { MANIFESTS+=("$PYPROJECT"); TARGETS+=("toml"); }

# Cargo.toml: the one with a top-level [package] version (rust/ or root). Skip target/.
mapfile -t _cg < <(find "$ROOT" -name Cargo.toml -not -path '*/target/*' -not -path '*/node_modules/*' 2>/dev/null \
                   | while read -r f; do grep -qE '^version *= *"' "$f" && echo "$f"; done)
CARGO=""
if [ "${#_cg[@]}" -eq 1 ]; then CARGO="${_cg[0]}"; MANIFESTS+=("$CARGO"); TARGETS+=("toml")
elif [ "${#_cg[@]}" -gt 1 ]; then die "multiple versioned Cargo.toml found; pick one: ${_cg[*]}"; fi

mapfile -t _pj < <(find "$ROOT" -path '*/.claude-plugin/plugin.json' -not -path '*/node_modules/*' 2>/dev/null)
[ "${#_pj[@]}" -eq 1 ] || die "expected exactly one .claude-plugin/plugin.json, found ${#_pj[@]}: ${_pj[*]:-none}"
PLUGIN_JSON="${_pj[0]}"; MANIFESTS+=("$PLUGIN_JSON"); TARGETS+=("json")

OPENCODE_PKG="$ROOT/plugins/opencode/package.json"
if [ ! -f "$OPENCODE_PKG" ]; then
  mapfile -t _oc < <(find "$ROOT/plugins" -maxdepth 2 -name package.json -not -path '*/node_modules/*' 2>/dev/null)
  [ "${#_oc[@]}" -eq 1 ] || die "could not uniquely locate the opencode package.json"
  OPENCODE_PKG="${_oc[0]}"
fi
MANIFESTS+=("$OPENCODE_PKG"); TARGETS+=("json")

# ---- args -------------------------------------------------------------------
do_commit=1; do_tag=1; dry_run=0; arg=""
for a in "$@"; do case "$a" in
  --no-tag)     do_tag=0 ;;
  --no-commit)  do_commit=0; do_tag=0 ;;
  -n|--dry-run) dry_run=1 ;;
  -h|--help)    sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
  -*)           die "unknown option: $a" ;;
  *)            [ -z "$arg" ] || die "unexpected extra argument: $a"; arg="$a" ;;
esac; done
[ -n "$arg" ] || die "need a version or bump type (X.Y.Z | patch | minor | major); see --help"

read_ver() { # $1=file $2=kind
  case "$2" in
    toml) grep -E '^version *= *"' "$1" | head -1 | sed -E 's/.*"([^"]+)".*/\1/' ;;
    json) grep -E '"version" *:'    "$1" | head -1 | sed -E 's/.*"version" *: *"([^"]+)".*/\1/' ;;
  esac
}
write_ver() { # $1=file $2=kind $3=new
  case "$2" in
    toml) sed -i -E "0,/^version *= *\"[^\"]+\"/s//version = \"$3\"/" "$1" ;;
    json) sed -i -E "0,/\"version\" *: *\"[^\"]+\"/s//\"version\": \"$3\"/" "$1" ;;
  esac
}

highest() { printf '%s\n' "$@" | sort -t. -k1,1n -k2,2n -k3,3n | tail -1; }

curs=(); for i in "${!MANIFESTS[@]}"; do
  v="$(read_ver "${MANIFESTS[$i]}" "${TARGETS[$i]}")"
  [ -n "$v" ] || die "could not read version from ${MANIFESTS[$i]}"
  curs+=("$v")
done
base="$(highest "${curs[@]}")"

case "$arg" in
  major|minor|patch)
    IFS=. read -r M m p <<<"$base"
    [[ "$M" =~ ^[0-9]+$ && "$m" =~ ^[0-9]+$ && "$p" =~ ^[0-9]+$ ]] || die "baseline '$base' not X.Y.Z; pass an explicit version"
    case "$arg" in major) M=$((M+1)); m=0; p=0 ;; minor) m=$((m+1)); p=0 ;; patch) p=$((p+1)) ;; esac
    new="$M.$m.$p" ;;
  *) [[ "$arg" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "'$arg' is not a valid X.Y.Z version"; new="$arg" ;;
esac
TAG="v$new"

echo "manifests:"
for i in "${!MANIFESTS[@]}"; do printf '  %-7s %s  (%s)\n' "${curs[$i]}" "${MANIFESTS[$i]#"$ROOT"/}" "${TARGETS[$i]}"; done
echo "baseline (max) : $base"
echo "new version    : $new   (tag $TAG)"

if [ "$(highest "$base" "$new")" = "$base" ] && [ "$new" != "$base" ]; then
  die "new version $new is lower than baseline $base; refusing to go backwards"
fi

if [ "$dry_run" = 1 ]; then echo "(dry run) no files changed"; exit 0; fi

if [ "$do_commit" = 1 ]; then
  excl=(); for f in "${MANIFESTS[@]}"; do excl+=( ":!${f#"$ROOT"/}" ); done
  if [ -n "$(git -C "$ROOT" status --porcelain -- "${excl[@]}")" ]; then
    die "working tree has unrelated changes; commit or stash them first"
  fi
fi

if [ "$do_tag" = 1 ] && git -C "$ROOT" rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  die "tag $TAG already exists"
fi

for i in "${!MANIFESTS[@]}"; do write_ver "${MANIFESTS[$i]}" "${TARGETS[$i]}" "$new"; done
echo "updated ${#MANIFESTS[@]} manifests -> $new"

[ "$do_commit" = 0 ] && { echo "files updated; skipped commit (--no-commit)"; exit 0; }

git -C "$ROOT" add "${MANIFESTS[@]}"
git -C "$ROOT" commit -m "release: $TAG — lockstep version across all manifests"
echo "committed."

if [ "$do_tag" = 1 ]; then
  git -C "$ROOT" tag -a "$TAG" -m "$new"
  echo "tagged $TAG"
  echo; echo "push with:  git push && git push origin $TAG"
else
  echo "skipped tag (--no-tag)"
fi
