#!/usr/bin/env bash
# Build the Docker image on this computer, push it to Docker Hub with the next version tag,
# optionally roll Koyeb onto it. macOS / Linux terminal, or Git Bash on Windows.
#
#   ./release.sh                     # next patch from Hub+VERSION (e.g. v1.0.1 -> v1.0.2)
#   ./release.sh minor               # next minor
#   ./release.sh major               # next major
#   ./release.sh --tag v2.0.0-rc1    # explicit tag
#   ./release.sh --koyeb distractors/web --koyeb distractors/runner
#   ./release.sh --local             # build for this machine only, no push, no bump
#   ./release.sh --dry-run           # print the commands without running them
#
# Other options: --platforms linux/amd64 (faster; enough for Koyeb), --no-cache, --force
# (overwrite an existing tag), --no-latest (leave :latest alone), --repo user/name.
#
# Version tracking: the next tag is computed from the *higher* of (1) the VERSION file and
# (2) the highest vX.Y.Z already on Docker Hub, so a stale local VERSION never collides.
# After a successful release, VERSION is updated; commit it.
set -euo pipefail
cd "$(dirname "$0")"

REPO="${DOCKER_REPO:-rahadarmannabid/distractors}"
PLATFORMS="${DOCKER_PLATFORMS:-linux/amd64,linux/arm64}"   # Koyeb runs amd64, Apple Silicon arm64
BUILDER="distractors-multiarch"
PART="patch"
TAG=""
LOCAL=0 DRY_RUN=0 FORCE=0 NO_LATEST=0 NO_CACHE=0
KOYEB_SERVICES=()

info() { printf '==> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

# Print a command, then run it (unless --dry-run).
run() {
  printf '  $'; printf ' %s' "$@"; printf '\n'
  [[ "$DRY_RUN" == 1 ]] || "$@"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    patch|minor|major) PART="$1" ;;
    --tag)       TAG="${2:?--tag needs a value}"; shift ;;
    --repo)      REPO="${2:?--repo needs a value}"; shift ;;
    --platforms) PLATFORMS="${2:?--platforms needs a value}"; shift ;;
    --koyeb)     KOYEB_SERVICES+=("${2:?--koyeb needs APP/SERVICE}"); shift ;;
    --local)     LOCAL=1 ;;
    --dry-run)   DRY_RUN=1 ;;
    --force)     FORCE=1 ;;
    --no-latest) NO_LATEST=1 ;;
    --no-cache)  NO_CACHE=1 ;;
    -h|--help)   usage ;;
    *)           die "unknown option: $1 (see ./release.sh --help)" ;;
  esac
  shift
done

command -v docker >/dev/null || die "docker not found. Install Docker Desktop (macOS/Windows) or Docker Engine (Linux)."
docker info >/dev/null 2>&1 || die "Docker is not running. Start Docker Desktop (or the docker service) and try again."

# ── Version helpers ─────────────────────────────────────────────────────────
SEMVER='^v?([0-9]+)\.([0-9]+)\.([0-9]+)$'

# Highest plain semver tag on Docker Hub for $REPO (e.g. 1.0.1), or empty if none / unreachable.
hub_latest_version() {
  local ns name
  ns="${REPO%%/*}"
  name="${REPO#*/}"
  python3 - "$ns" "$name" <<'PY' 2>/dev/null || true
import json, re, sys, urllib.request

ns, name = sys.argv[1], sys.argv[2]
pat = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
best = None  # (maj, min, pat, "x.y.z")
url = f"https://hub.docker.com/v2/repositories/{ns}/{name}/tags?page_size=100&ordering=last_updated"
while url:
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.load(resp)
    except Exception:
        break
    for row in data.get("results") or []:
        m = pat.match(str(row.get("name") or ""))
        if not m:
            continue
        trip = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        label = f"{trip[0]}.{trip[1]}.{trip[2]}"
        if best is None or trip > best[:3]:
            best = (*trip, label)
    url = data.get("next")
if best:
    print(best[3])
PY
}

# Compare two x.y.z strings; print the higher (empty counts as 0.0.0).
max_version() {
  python3 - "$1" "$2" <<'PY'
import re, sys
pat = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
def parse(s):
    m = pat.match((s or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)
a, b = parse(sys.argv[1]), parse(sys.argv[2])
best = a if a >= b else b
print(f"{best[0]}.{best[1]}.{best[2]}")
PY
}

bump_version() {
  local current="$1" part="$2"
  [[ "$current" =~ $SEMVER ]] || die "cannot bump version '$current'"
  local major="${BASH_REMATCH[1]}" minor="${BASH_REMATCH[2]}" patch="${BASH_REMATCH[3]}"
  case "$part" in
    major) echo "v$((major + 1)).0.0" ;;
    minor) echo "v${major}.$((minor + 1)).0" ;;
    patch) echo "v${major}.${minor}.$((patch + 1))" ;;
    *) die "unknown bump part: $part" ;;
  esac
}

tag_exists_on_hub() {
  docker buildx imagetools inspect "$1" >/dev/null 2>&1
}

# ── Version ─────────────────────────────────────────────────────────────────
if [[ -n "$TAG" ]]; then
  [[ "$TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || die "invalid Docker tag: $TAG"
elif [[ "$LOCAL" == 1 ]]; then
  TAG="local"
else
  local_ver="0.0.0"
  if [[ -f VERSION ]]; then
    local_ver="$(tr -d '[:space:]' < VERSION)"
    [[ "$local_ver" =~ $SEMVER ]] || die "VERSION must hold a version like 1.2.3 (found '$local_ver')"
    local_ver="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.${BASH_REMATCH[3]}"
  fi
  info "Checking Docker Hub for latest ${REPO} semver tag…"
  hub_ver="$(hub_latest_version || true)"
  if [[ -n "${hub_ver}" ]]; then
    info "Docker Hub latest: v${hub_ver}  |  local VERSION: v${local_ver}"
  else
    info "No Hub semver found (or Hub unreachable); using local VERSION: v${local_ver}"
    hub_ver="0.0.0"
  fi
  current="$(max_version "$local_ver" "$hub_ver")"
  TAG="$(bump_version "$current" "$PART")"
  # If that tag somehow already exists, keep patch-bumping until free (unless --force).
  if [[ "$FORCE" != 1 ]]; then
    guard=0
    while tag_exists_on_hub "${REPO}:${TAG}"; do
      guard=$((guard + 1))
      [[ "$guard" -le 50 ]] || die "could not find a free tag after ${TAG}; pass --tag or --force"
      info "${REPO}:${TAG} already on Hub — bumping again"
      TAG="$(bump_version "${TAG#v}" patch)"
    done
  fi
  info "Next release tag: ${TAG}  (from v${current} + ${PART})"
fi
IMAGE="${REPO}:${TAG}"

BUILD=(docker buildx build -f Dockerfile --build-arg "APP_VERSION=${TAG}" -t "$IMAGE")
[[ "$LOCAL" == 1 || "$NO_LATEST" == 1 ]] || BUILD+=(-t "${REPO}:latest")
[[ "$NO_CACHE" == 1 ]] && BUILD+=(--no-cache)

# ── Local build (this machine only) ─────────────────────────────────────────
if [[ "$LOCAL" == 1 ]]; then
  info "Local build of ${IMAGE} (this machine's architecture, not pushed)"
  run "${BUILD[@]}" --load .
  info "Done. Try it:  docker run --rm -p 8000:8000 --env-file .env ${IMAGE}"
  exit 0
fi

# ── Multi-arch build + push ─────────────────────────────────────────────────
docker buildx version >/dev/null 2>&1 || die "docker buildx not found. It ships with Docker Desktop; on Linux install docker-buildx-plugin."
if [[ "$FORCE" != 1 ]] && tag_exists_on_hub "$IMAGE"; then
  die "${IMAGE} already exists on Docker Hub. Pass --force to overwrite, or omit --tag to auto-bump."
fi

info "Releasing ${IMAGE}  (platforms: ${PLATFORMS})"
echo "    If the push is denied, run 'docker login' (user: rahadarmannabid) and retry."

# Multi-arch builds need a docker-container builder; the default "docker" driver cannot do them.
if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
  run docker buildx create --name "$BUILDER" --driver docker-container --bootstrap
fi
run "${BUILD[@]}" --builder "$BUILDER" --platform "$PLATFORMS" --push .

if [[ "$TAG" =~ $SEMVER && "$DRY_RUN" != 1 ]]; then
  echo "${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.${BASH_REMATCH[3]}" > VERSION
  info "VERSION is now $(cat VERSION) (commit it)"
fi

# ── Optional: point Koyeb services at the new tag ───────────────────────────
if [[ ${#KOYEB_SERVICES[@]} -gt 0 ]]; then
  command -v koyeb >/dev/null || die "pushed ${IMAGE}, but the koyeb CLI is not installed; update the services in the Koyeb dashboard."
  for svc in "${KOYEB_SERVICES[@]}"; do
    info "Deploying ${IMAGE} to Koyeb service ${svc}"
    run koyeb service update "$svc" --docker "$IMAGE"
  done
fi

info "Done: ${IMAGE}$([[ "$NO_LATEST" == 1 ]] || echo " and ${REPO}:latest")"
echo "    docker pull ${IMAGE}"
