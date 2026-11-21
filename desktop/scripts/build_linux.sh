#!/usr/bin/env bash
# Build, policy-check, package, and smoke-verify the Linux x86_64 release tarball.
# Never installs udev rules, alters host audio routing, or publishes artifacts.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${DESKTOP_DIR}/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python3}"
MATERIALS_DIR="${MATERIALS_DIR:-${REPO_ROOT}/artifacts/release-materials-linux}"
DIST_DIR="${DIST_DIR:-${DESKTOP_DIR}/dist/linux}"
VERSION="$(tr -d '[:space:]' < "${DESKTOP_DIR}/VERSION")"
ARCHIVE_OUT="${ARCHIVE_OUT:-${DIST_DIR}/10RemoteHub-${VERSION}-linux-x86_64.tar.gz}"

cd "${DESKTOP_DIR}"
export QT_QPA_PLATFORM=offscreen

"${PYTHON_BIN}" scripts/inventory_linux_runtime.py --output-dir "${MATERIALS_DIR}"
"${PYTHON_BIN}" scripts/prepare_linux_licenses.py --output-dir "${MATERIALS_DIR}" "$@"
"${PYTHON_BIN}" scripts/build.py \
  --dist-dir "${DIST_DIR}" \
  --license-supplement "${MATERIALS_DIR}/license-supplement-linux.json" \
  --dependency-manifest "${MATERIALS_DIR}/dependencies-linux.json"

rm -f "${ARCHIVE_OUT}"
"${PYTHON_BIN}" scripts/package_linux_tar.py "${DIST_DIR}/10RemoteHub" "${ARCHIVE_OUT}"
"${PYTHON_BIN}" scripts/release_policy.py "${ARCHIVE_OUT}"

TMP_VERIFY="$(mktemp -d -t rh-linux-verify-XXXXXX)"
trap 'rm -rf "${TMP_VERIFY}"' EXIT
tar -xzf "${ARCHIVE_OUT}" -C "${TMP_VERIFY}"
"${TMP_VERIFY}/10RemoteHub/10RemoteHub" --smoke --config-dir "${TMP_VERIFY}/cfg" --screenshot "${TMP_VERIFY}/smoke.png"
"${TMP_VERIFY}/10RemoteHub/10RemoteHub" --diagnose "${TMP_VERIFY}/diagnose.json"
"${TMP_VERIFY}/10RemoteHub/10RemoteHub" --permission-status > "${TMP_VERIFY}/permissions.json"

sha256sum "${ARCHIVE_OUT}"
echo "Linux candidate verified: ${ARCHIVE_OUT}"
