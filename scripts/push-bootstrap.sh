#!/usr/bin/env bash
# Create a repo from bootstrap/<name> and push it. Usage: scripts/push-bootstrap.sh <owner> <name> [--internal|--private|--public]
# The default is --private. The repo's visibility is verified (allowlist) BEFORE any content is pushed.
# --public is the personal proof-of-concept exception only. It is refused unless GOLDEN_ROAD_PUBLIC_POC equals the
# owner's login, that login is the authenticated gh user, AND GitHub reports the owner as a personal account (type
# exactly "User", never an Organization); GH_HOST is pinned to github.com. All of it runs before anything is created. See docs/setup/personal-poc.md.
set -euo pipefail
[ "$#" -ge 2 ] && [ "$#" -le 3 ] || { echo "usage: $0 <org> <name> [--internal|--private|--public]" >&2; exit 2; }
org="$1"; name="$2"; visibility="${3:---private}"
case "$visibility" in --internal|--private|--public) ;; *) echo "visibility must be --internal, --private, or --public (personal PoC only)" >&2; exit 2;; esac
[[ "$org" =~ ^[A-Za-z0-9]+(-[A-Za-z0-9]+)*$ ]] || { echo "bad org: $org" >&2; exit 2; }
[[ "$name" =~ ^[A-Za-z0-9]+([._-][A-Za-z0-9]+)*$ ]] || { echo "bad name: $name" >&2; exit 2; }
if [ "$visibility" = "--public" ]; then
  export GH_HOST=github.com   # never a GitHub Enterprise host
  [ "${GOLDEN_ROAD_PUBLIC_POC:-}" = "$org" ] || { echo "--public is refused: it is the personal PoC exception. Set GOLDEN_ROAD_PUBLIC_POC=$org (the owner's login) to opt in (docs/setup/personal-poc.md)." >&2; exit 2; }
  login=$(gh api user --jq .login) || login=""
  [ "$login" = "$org" ] || { echo "--public is refused: $org is not the authenticated gh user (${login:-unknown})." >&2; exit 1; }
  owner_type=$(gh api "users/$org" --jq .type) || owner_type=""
  [ "$owner_type" = "User" ] || { echo "--public is refused: $org is not a personal account (type '${owner_type:-unknown}'). Organizations stay private or internal." >&2; exit 1; }
fi
src="$(cd "$(dirname "$0")/.." && pwd)/bootstrap/$name"
[ -d "$src" ] || { echo "no bootstrap/$name" >&2; exit 2; }

rc=0
grep -rqE 'replace-me|ORG_PLACEHOLDER' "$src" || rc=$?
if [ "$rc" -eq 0 ]; then echo "bootstrap/$name still has placeholder values (replace-me or ORG_PLACEHOLDER). Run scripts/configure.py first." >&2; exit 2; fi
[ "$rc" -eq 1 ] || { echo "could not scan bootstrap/$name for placeholders (grep exit $rc)" >&2; exit 2; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cp -a "$src/." "$work/"
cd "$work"
git init -q -b main
git add -A
git commit -q -m "Bootstrap $name from platform-pipeline"

# Create empty (nothing is pushed yet), then verify the visibility before pushing any content.
gh repo create "$org/$name" "$visibility"
actual=$(gh repo view "$org/$name" --json visibility --jq .visibility) || actual=""
if [ "$visibility" = "--public" ]; then expect="PUBLIC"; else expect="PRIVATE or INTERNAL"; fi
case "$visibility:$actual" in
  --public:PUBLIC|--private:PRIVATE|--private:INTERNAL|--internal:PRIVATE|--internal:INTERNAL) ;;
  *)
    echo "ERROR: $org/$name has visibility '${actual:-unknown}', not $expect. Nothing was pushed." >&2
    echo "ERROR: Fix or delete the repository $org/$name now." >&2
    exit 1;;
esac
url=$(gh repo view "$org/$name" --json url --jq .url)
git -c credential.helper= -c credential.helper='!gh auth git-credential' push -u "$url" main
echo "pushed $org/$name ($actual)"
