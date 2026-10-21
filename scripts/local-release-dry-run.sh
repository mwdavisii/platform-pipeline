#!/usr/bin/env bash
# Run the release steps locally: drift -> build -> authz probe -> render. No signing, no push.
# Usage: scripts/local-release-dry-run.sh <app-dir> [--server-dry-run]
set -euo pipefail
app="$(cd "$1" && pwd)"; shift || true
root="$(cd "$(dirname "$0")/.." && pwd)"
net=(); [ -n "${GOLDEN_ROAD_DOCKER_BUILD_NETWORK:-}" ] && net=(--network "$GOLDEN_ROAD_DOCKER_BUILD_NETWORK")
out="$(mktemp -d)"; chmod 777 "$out"
builder=golden-road-builder:dry-run
docker build -q "${net[@]}" -f "$root/images/builder/Dockerfile" -t "$builder" "$root" > /dev/null
gr() { docker run --rm --network none --read-only --tmpfs /tmp --user "$(id -u):$(id -g)" -v "$app:/src:ro" -v "$out:/out" "$builder" "$@"; }

echo "== drift"
gr golden-road preflight /src --output /out/preflight.json
set +e; gr builder verify /src > "$out/verify.json"; code=$?; set -e
[ "$code" -eq 0 ] || { echo "builder verify exited $code"; cat "$out/verify.json"; exit 1; }

echo "== build"
app_name=$(jq -r .app_name "$app/builder-spec.json")
docker build -q "${net[@]}" -t "golden-road-dry-run/$app_name" "$app" > /dev/null
digest=$(docker image inspect --format '{{.Id}}' "golden-road-dry-run/$app_name")
ref="localhost/$app_name@$digest"

echo "== authz probe"
port=$(jq -r .port "$app/builder-spec.json")
cid=$(docker run -d --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges "golden-road-dry-run/$app_name")
trap 'docker rm -f "$cid" > /dev/null' EXIT
docker run --rm --network "container:$cid" --read-only --tmpfs /tmp --user "$(id -u):$(id -g)" -v "$app:/src:ro" -v "$out:/out" "$builder" \
  golden-road authz-probe --repo-root /src --base-url "http://127.0.0.1:$port" --output /out/authz.json
jq -c '{status, cases, denied}' "$out/authz.json"

echo "== render"
gr golden-road render --repo-root /src --image "$ref" --output /out/manifests.yaml
echo "manifests: $out/manifests.yaml"
if [ "${1:-}" = "--server-dry-run" ]; then
  echo "== kubectl apply --dry-run=server"
  kubectl apply --dry-run=server -n default -f "$out/manifests.yaml"
fi
