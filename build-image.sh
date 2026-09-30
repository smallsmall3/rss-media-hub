#!/usr/bin/env bash
# ============================================================================
#  Build the Docker image and export it as a tar, for importing into
#  UGREEN NAS (Docker -> Images -> Import).
#
#  ASCII-only on purpose: keeps the file safe regardless of the shell locale
#  or the editor used to save it.
#
#  Usage:
#      chmod +x build-image.sh
#      ./build-image.sh                          # default rss-media-hub:1.0.0
#      ./build-image.sh myrepo/rss:1.0.0         # custom image name
#      PLATFORM=linux/amd64 ./build-image.sh     # force target arch
#      NOCACHE=1 ./build-image.sh                # rebuild without cache
# ============================================================================

set -euo pipefail

TAG="${1:-rss-media-hub:1.0.0}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAFE_NAME="$(echo "$TAG" | tr ':/\\' '---')"
OUT="${OUT:-$ROOT/$SAFE_NAME.tar}"
PLATFORM="${PLATFORM:-}"
NOCACHE="${NOCACHE:-0}"

step() { printf '\n==> %s\n' "$1"; }
ok()   { printf '    %s\n' "$1"; }
note() { printf '    %s\n' "$1"; }

step "Checking Docker"
if ! command -v docker >/dev/null 2>&1; then
    echo "docker command not found. Install Docker first." >&2
    exit 1
fi
if ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
    echo "Docker daemon is not running. Start Docker first." >&2
    exit 1
fi
SERVER_VERSION="$(docker version --format '{{.Server.Version}}')"
ok "Docker OK, server version $SERVER_VERSION"

if [ ! -f "$ROOT/Dockerfile" ]; then
    echo "Dockerfile not found in: $ROOT" >&2
    exit 1
fi

HOST_ARCH="$(docker version --format '{{.Server.Arch}}' 2>/dev/null || echo unknown)"
step "Architecture"
ok "Docker server arch: $HOST_ARCH"
if [ -n "$PLATFORM" ]; then
    note "Forcing target platform: $PLATFORM"
elif [ "$HOST_ARCH" = "amd64" ]; then
    note "UGREEN NAS is usually x86_64 (amd64) - this build is compatible."
else
    note "If your UGREEN NAS is x86_64, rebuild with: PLATFORM=linux/amd64 ./build-image.sh $TAG"
fi

step "Building image $TAG (first build takes 2-5 minutes)"
BUILD_ARGS=(build -t "$TAG")
[ -n "$PLATFORM" ] && BUILD_ARGS+=(--platform "$PLATFORM")
[ "$NOCACHE" = "1" ] && BUILD_ARGS+=(--no-cache)
BUILD_ARGS+=("$ROOT")
docker "${BUILD_ARGS[@]}"
ok "Build done"

step "Exporting tar: $OUT"
docker save "$TAG" -o "$OUT"
SIZE_MB="$(du -m "$OUT" | cut -f1)"
ok "Export done, file size: ${SIZE_MB} MB"

cat <<EOF

================================================================================
 Next: import into UGREEN NAS
================================================================================
 1. Copy this file to the NAS (UGREEN Files upload, or SMB share):
      $OUT

 2. UGREEN Docker -> Images -> Local images -> Import -> pick the tar file
    Wait 1-3 minutes for the import to finish.

 3. UGREEN Docker -> Project -> Create -> paste compose.yaml from the repo,
    then set the image line to:
      image: $TAG
    Fill in Telegram / TMDB / Emby values and your storage folders, then deploy.

 4. Check the container log: 'Telegram bot: @xxx' means it works.
================================================================================
EOF
