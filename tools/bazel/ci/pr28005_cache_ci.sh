#!/bin/bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
baseline=4904e6bc67c279a29f2aafd6541412b76d235687
if ! git merge-base --is-ancestor "$baseline" HEAD; then
    echo "Checkout must descend from PR #28005 baseline $baseline" >&2
    exit 1
fi
if ! git diff --quiet --ignore-submodules=none || ! git diff --cached --quiet; then
    echo "Tracked sources and submodule commits must be clean" >&2
    exit 1
fi
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
    echo "PR #28005 cache validation requires Linux x86_64" >&2
    exit 1
fi
if [[ ! "${BAZEL_REMOTE_CACHE_URL:-}" =~ ^grpcs://[^/:[:space:]]+:[0-9]+(/[^[:space:]]*)?$ ]]; then
    echo "BAZEL_REMOTE_CACHE_URL must contain a grpcs:// host and port" >&2
    exit 1
fi
: "${AGENT_TEMPDIRECTORY:?}" "${CLIENT_CERT_ARCHIVE:?}" "${PR28005_RESULTS_DIR:?}"
mkdir "$PR28005_RESULTS_DIR"
echo "##vso[task.setvariable variable=PR28005_RESULTS_READY]true"
git submodule status --recursive > "$PR28005_RESULTS_DIR/submodules.txt"
if grep -qE '^[+U-]' "$PR28005_RESULTS_DIR/submodules.txt"; then
    echo "All submodules must be initialized at their recorded commits" >&2
    exit 1
fi

run_root="$(mktemp -d "$AGENT_TEMPDIRECTORY/pr28005-cache.XXXXXXXX")"
cleanup() {
    local status=$?
    # Hermetic SDK directories can be read-only; do not follow cache symlinks.
    find "$run_root" -type d -exec chmod u+w {} +
    rm -rf -- "$run_root"
    return "$status"
}
trap cleanup EXIT
umask 077
mkdir "$run_root/certs" "$run_root/cache"
members="$(tar -tzf "$CLIENT_CERT_ARCHIVE")"
for name in server-ca.crt writer-client.crt writer-client.key; do
    if ! member="$(printf '%s\n' "$members" | grep -xE "(\./)?${name//./\\.}")" ||
        [[ "$member" == *$'\n'* ]]; then
        echo "Expected one certificate archive member: $name" >&2
        exit 1
    fi
    tar -xOzf "$CLIENT_CERT_ARCHIVE" -- "$member" > "$run_root/certs/$name"
    if [[ ! -s "$run_root/certs/$name" ]]; then
        echo "Missing or empty certificate file: $name" >&2
        exit 1
    fi
done

cat > "$run_root/remote.bazelrc" <<EOF
startup --batch
common --disk_cache=
common --remote_cache=$BAZEL_REMOTE_CACHE_URL
common --remote_instance_name=sonic-buildimage/e2e/trixie-amd64
common --remote_executor=
common --remote_accept_cached=true
common --remote_upload_local_results=true
common --remote_cache_async=false
common --remote_verify_downloads=true
common --remote_download_outputs=all
common --remote_timeout=120
common --remote_retries=5
common --tls_certificate=/etc/sonic-bazel-certs/server-ca.crt
common --tls_client_certificate=/etc/sonic-bazel-certs/writer-client.crt
common --tls_client_key=/etc/sonic-bazel-certs/writer-client.key
build --jobs=4
EOF

make_options=(
    NOBOOKWORM=1 NOTRIXIE=0 BAZEL_MIN_READINESS=experimental
    SONIC_DPKG_CACHE_METHOD=none SONIC_CONFIG_USE_DOCKER_CACHE=n
    SONIC_BUILD_JOBS=4 USERNAME=admin "BUILD_NUMBER=${BUILD_BUILDID:?}"
    "SONIC_VERSION_CONTROL_COMPONENTS=py2,py3,web,git,docker"
    "SONIC_BAZEL_CACHE_SOURCE=$run_root/cache"
    "SONIC_BAZEL_REMOTE_CACHE_CONFIG=$run_root/remote.bazelrc"
    "DOCKER_BUILDER_USER_MOUNT=$run_root/certs:/etc/sonic-bazel-certs:ro,$PR28005_RESULTS_DIR:/pr28005-results:rw"
)
ENABLE_DOCKER_BASE_PULL=y make "${make_options[@]}" configure PLATFORM=vs PLATFORM_ARCH=amd64
make "${make_options[@]}" target/docker-config-engine-trixie.gz \
    target/docker-sysmgr.manifest.json target/docker-sysmgr-dbg.manifest.json

export PR28005_BASELINE="$baseline"
python3 - "$PR28005_RESULTS_DIR/provenance.json" <<'PY'
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, "tools/bazel/ci")
from verify_pr28005_cache import file_hash

Path(sys.argv[1]).write_text(json.dumps({
    "pr": "https://github.com/sonic-net/sonic-buildimage/pull/28005",
    "baseline": os.environ["PR28005_BASELINE"],
    "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "bazel_version": Path(".bazelversion").read_text().strip(),
    "config_engine_base_sha256": file_hash("target/docker-config-engine-trixie.gz"),
    "readiness": "experimental",
    "dpkg_cache": "none",
    "remote_instance": "sonic-buildimage/e2e/trixie-amd64",
}, indent=2) + "\n")
PY

make "${make_options[@]}" sonic-slave-run \
    SONIC_RUN_CMDS="cd /sonic && bash tools/bazel/ci/validate_pr28005_cache.sh /pr28005-results"
# docker-start is phony, so pin the already-built base for this job's integration
# pass. SONIC_BUILD_VARS forwards the Make option into the actual slave recipe.
make "${make_options[@]}" \
    SONIC_BUILD_VARS=--old-file=target/docker-config-engine-trixie.gz target/sonic-vs.img.gz
gzip -t target/sonic-vs.img.gz
python3 tools/bazel/ci/verify_pr28005_cache.py integration \
    "$PR28005_RESULTS_DIR/cache-validation.json" target/docker-sysmgr.gz \
    target/sonic-vs.img.gz
cp target/sonic-vs.img.gz "$PR28005_RESULTS_DIR/"
