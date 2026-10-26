#!/bin/bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
if ! git diff --quiet --ignore-submodules=none || ! git diff --cached --quiet; then
    echo "Tracked sources and submodule commits must be clean" >&2
    exit 1
fi
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
    echo "Remote-cache validation requires Linux x86_64" >&2
    exit 1
fi
: "${AGENT_TEMPDIRECTORY:?}" "${CLIENT_CERT_ARCHIVE:?}" "${BAZEL_CACHE_RESULTS_DIR:?}"
mkdir "$BAZEL_CACHE_RESULTS_DIR"
echo "##vso[task.setvariable variable=BAZEL_CACHE_RESULTS_READY]true"
git submodule status --recursive > "$BAZEL_CACHE_RESULTS_DIR/submodules.txt"
if grep -qE '^[+U-]' "$BAZEL_CACHE_RESULTS_DIR/submodules.txt"; then
    echo "All submodules must be initialized at their recorded commits" >&2
    exit 1
fi

run_root="$(mktemp -d "$AGENT_TEMPDIRECTORY/bazel-remote-cache.XXXXXXXX")"
cleanup() {
    local status=$?
    # Hermetic SDK directories can be read-only; do not follow cache symlinks.
    find "$run_root" -type d -exec chmod u+w {} +
    rm -rf -- "$run_root"
    return "$status"
}
trap cleanup EXIT
umask 077
cache_dir="$run_root/remote"
BAZEL_REMOTE_INSTANCE_NAME=sonic-buildimage/e2e/trixie-amd64 \
    bash tools/bazel/ci/configure_remote_cache.sh "$cache_dir"
cat >> "$cache_dir/remote.bazelrc" <<EOF
common --disk_cache=
build --jobs=4
EOF

make_options=(
    NOBOOKWORM=1 NOTRIXIE=0 BAZEL_MIN_READINESS=experimental
    SONIC_DPKG_CACHE_METHOD=none SONIC_CONFIG_USE_DOCKER_CACHE=n
    SONIC_BUILD_JOBS=4 USERNAME=admin "BUILD_NUMBER=${BUILD_BUILDID:?}"
    "SONIC_VERSION_CONTROL_COMPONENTS=py2,py3,web,git,docker"
    "SONIC_BAZEL_CACHE_SOURCE=$cache_dir/cache"
    "SONIC_BAZEL_REMOTE_CACHE_CONFIG=$cache_dir/remote.bazelrc"
    "DOCKER_BUILDER_USER_MOUNT=$cache_dir/certs:/etc/sonic-bazel-certs:ro,$BAZEL_CACHE_RESULTS_DIR:/bazel-cache-results:rw"
)
ENABLE_DOCKER_BASE_PULL=y make "${make_options[@]}" configure PLATFORM=vs PLATFORM_ARCH=amd64
make "${make_options[@]}" target/docker-config-engine-trixie.gz \
    target/docker-sysmgr.manifest.json target/docker-sysmgr-dbg.manifest.json

python3 - "$BAZEL_CACHE_RESULTS_DIR/provenance.json" <<'PY'
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, "tools/bazel/ci")
from verify_remote_cache import file_hash

Path(sys.argv[1]).write_text(json.dumps({
    "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "bazel_version": Path(".bazelversion").read_text().strip(),
    "config_engine_base_sha256": file_hash("target/docker-config-engine-trixie.gz"),
    "readiness": "experimental",
    "dpkg_cache": "none",
    "remote_instance": "sonic-buildimage/e2e/trixie-amd64",
}, indent=2) + "\n")
PY

make "${make_options[@]}" sonic-slave-run \
    SONIC_RUN_CMDS="cd /sonic && bash tools/bazel/ci/validate_remote_cache.sh /bazel-cache-results"
# docker-start is phony, so pin the already-built base for this job's integration
# pass. SONIC_BUILD_VARS forwards the Make option into the actual slave recipe.
make "${make_options[@]}" \
    SONIC_BUILD_VARS=--old-file=target/docker-config-engine-trixie.gz target/sonic-vs.img.gz
gzip -t target/sonic-vs.img.gz
python3 tools/bazel/ci/verify_remote_cache.py integration \
    "$BAZEL_CACHE_RESULTS_DIR/cache-validation.json" target/docker-sysmgr.gz \
    target/sonic-vs.img.gz
cp target/sonic-vs.img.gz "$BAZEL_CACHE_RESULTS_DIR/"
