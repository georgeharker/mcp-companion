#!/usr/bin/env bash
#
# Bump the whole repo's version in FULL LOCKSTEP, then commit and tag.
#
# Writes ONE version into EVERY version-bearing manifest this repo has:
#   - pyproject.toml            (version = "X.Y.Z")                  — if present
#   - Cargo.toml                ([package] version = "X.Y.Z")        — if present
#   - the Claude Code plugin    plugins/*/.claude-plugin/plugin.json ("version")
#   - the opencode plugin       plugins/opencode/package.json        ("version")
#   - the marketplace listing   .claude-plugin/marketplace.json      (plugins[].version)
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

# bash 3.2 — what macOS still ships as /bin/bash — has no `mapfile`, and the
# `declare -n` nameref that would replace it only arrived in 4.3. Read lines into
# a named array the portable way instead, so this script runs on a stock Mac and
# not just one with a Homebrew bash on PATH.
#   Usage: read_lines <arrayname> < <(cmd)
read_lines() { # $1 = array name
  local __arr="$1" __line
  eval "$__arr=()"
  while IFS= read -r __line; do eval "$__arr+=(\"\$__line\")"; done
}

# ---- locate manifests -------------------------------------------------------
MANIFESTS=()   # every file that gets the new version
TARGETS=()     # parallel: "toml" | "json"

# pyproject.toml: the project's own (root OR a package subdir, e.g. combiner/),
# excluding vendored/submodule copies (vendor/, node_modules/).
read_lines _py < <(find "$ROOT" -name pyproject.toml \
                   -not -path '*/vendor/*' -not -path '*/node_modules/*' -not -path '*/target/*' -not -path '*/.git/*' 2>/dev/null \
                   | while read -r f; do grep -qE '^version *= *"' "$f" && echo "$f"; done)
if [ "${#_py[@]}" -eq 1 ]; then MANIFESTS+=("${_py[0]}"); TARGETS+=("toml")
elif [ "${#_py[@]}" -gt 1 ]; then die "multiple versioned pyproject.toml found; pick one: ${_py[*]}"; fi

# Cargo.toml: the one with a top-level [package] version (rust/ or root). Skip target/.
read_lines _cg < <(find "$ROOT" -name Cargo.toml -not -path '*/target/*' -not -path '*/node_modules/*' 2>/dev/null \
                   | while read -r f; do grep -qE '^version *= *"' "$f" && echo "$f"; done)
CARGO=""
if [ "${#_cg[@]}" -eq 1 ]; then CARGO="${_cg[0]}"; MANIFESTS+=("$CARGO"); TARGETS+=("toml")
elif [ "${#_cg[@]}" -gt 1 ]; then die "multiple versioned Cargo.toml found; pick one: ${_cg[*]}"; fi

read_lines _pj < <(find "$ROOT" -path '*/.claude-plugin/plugin.json' -not -path '*/node_modules/*' 2>/dev/null)
[ "${#_pj[@]}" -eq 1 ] || die "expected exactly one .claude-plugin/plugin.json, found ${#_pj[@]}: ${_pj[*]:-none}"
PLUGIN_JSON="${_pj[0]}"; MANIFESTS+=("$PLUGIN_JSON"); TARGETS+=("json")

OPENCODE_PKG="$ROOT/plugins/opencode/package.json"
if [ ! -f "$OPENCODE_PKG" ]; then
  read_lines _oc < <(find "$ROOT/plugins" -maxdepth 2 -name package.json -not -path '*/node_modules/*' 2>/dev/null)
  [ "${#_oc[@]}" -eq 1 ] || die "could not uniquely locate the opencode package.json"
  OPENCODE_PKG="${_oc[0]}"
fi
MANIFESTS+=("$OPENCODE_PKG"); TARGETS+=("json")

# The repo marketplace listing (.claude-plugin/marketplace.json) pins the Claude
# plugin's version in its plugins[] entry — it advertises the installable
# version to `/plugin marketplace`, so it must move in lockstep too. Its single
# "version" key is the plugin entry's (the manifest itself is unversioned).
MARKETPLACE_JSON="$ROOT/.claude-plugin/marketplace.json"
if [ -f "$MARKETPLACE_JSON" ] && grep -qE '"version" *:' "$MARKETPLACE_JSON"; then
  MANIFESTS+=("$MARKETPLACE_JSON"); TARGETS+=("json")
fi

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
  # NB: python3, not `sed -i -E "0,/re/s//../"`. That form is doubly GNU-only —
  # BSD/macOS sed reads `-i`'s argument as the backup suffix (so `-E` became one,
  # littering *-E files) and rejects the `0,/re/` address. The net effect on macOS
  # was a SILENT no-op: "updated N manifests" while every version stayed put, and
  # the follow-up commit then failed with "nothing added to commit". Replacing only
  # the first match, and erroring when there is none, keeps that failure loud.
  python3 - "$1" "$2" "$3" <<'PY'
import re, sys
path, kind, new = sys.argv[1:4]
pat = {"toml": r'^version *= *"[^"]+"', "json": r'"version" *: *"[^"]+"'}[kind]
rep = {"toml": f'version = "{new}"', "json": f'"version": "{new}"'}[kind]
src = open(path).read()
out, n = re.subn(pat, rep.replace("\\", "\\\\"), src, count=1, flags=re.M)
if n != 1:
    sys.exit(f"error: no {kind} version field matched in {path}")
open(path, "w").write(out)
PY
}

# Semver precedence, not plain numeric sort. A prerelease ranks BELOW the release it
# leads to (0.6.4-alpha.5 < 0.6.4) — `sort -k3,3n` reads both third fields as 4 and
# breaks the tie the wrong way, so promoting a prerelease to its final version was
# rejected as "going backwards".
highest() {
  printf '%s\n' "$@" | python3 -c '
import sys
def key(v):
    core, _, pre = v.strip().partition("-")
    nums = [int(x) if x.isdigit() else 0 for x in (core.split(".") + ["0", "0"])[:3]]
    # No prerelease sorts above any prerelease of the same core version.
    if not pre:
        return (nums, 1, [])
    # Dot-separated identifiers: numeric ones compare numerically and below alphas.
    ids = [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")]
    return (nums, 0, ids)
print(max((l for l in sys.stdin if l.strip()), key=key).strip())
'
}

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
  # A semver prerelease suffix is allowed (0.6.4-alpha.1, 1.0.0-rc.2). That is the
  # DEV RELEASE channel: cargo-dist marks any prerelease-suffixed tag as a GitHub
  # prerelease and — per the `announcement_is_prerelease` guard it generates — SKIPS
  # the crates.io publish job. So a prerelease tag exercises the full binary build,
  # installer generation and hosting with nothing irreversible happening on crates.io.
  # Only reachable as an explicit version; `patch`/`minor`/`major` never invent one.
  *) [[ "$arg" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z][0-9A-Za-z.-]*)?$ ]] \
       || die "'$arg' is not a valid X.Y.Z or X.Y.Z-prerelease version"; new="$arg" ;;
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

# Keep any Cargo.lock next to a bumped Cargo.toml in sync (the crate's own
# self-version entry, so a `--locked` build/publish doesn't fail).
LOCKS=()
for i in "${!MANIFESTS[@]}"; do
  [ "${TARGETS[$i]}" = "toml" ] || continue
  m="${MANIFESTS[$i]}"; case "$m" in */Cargo.toml) : ;; *) continue ;; esac
  lock="${m%/Cargo.toml}/Cargo.lock"; [ -f "$lock" ] || continue
  cname="$(grep -E '^name *= *"' "$m" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
  [ -n "$cname" ] || continue
  # `-i.bak` + rm: portable across GNU and BSD sed (bare `-i` differs — see write_ver).
  sed -i.bak "/^name = \"$cname\"\$/{n;s/^version = \"[^\"]*\"\$/version = \"$new\"/;}" "$lock"
  rm -f "$lock.bak"
  LOCKS+=("$lock"); echo "  synced $(basename "$lock") ($cname -> $new)"
done

[ "$do_commit" = 0 ] && { echo "files updated; skipped commit (--no-commit)"; exit 0; }

git -C "$ROOT" add "${MANIFESTS[@]}" ${LOCKS+"${LOCKS[@]}"}
git -C "$ROOT" commit -m "release: $TAG — lockstep version across all manifests"
echo "committed."

if [ "$do_tag" = 1 ]; then
  git -C "$ROOT" tag -a "$TAG" -m "$new"
  echo "tagged $TAG"
  echo; echo "push with:  git push && git push origin $TAG"
else
  echo "skipped tag (--no-tag)"
fi
