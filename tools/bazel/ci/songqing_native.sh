#!/bin/bash
set -euo pipefail
umask 077
export PATH=/usr/bin:/bin

target="${1:?Expected docker-sysmgr or sonic-vs-bin}"
case "$target" in
    docker-sysmgr) label=//dockers/docker-sysmgr:docker-sysmgr; required_gib=60 ;;
    sonic-vs-bin) label=//platform/vs:sonic-vs-bin; required_gib=120 ;;
    *) echo "Unsupported native target: $target" >&2; exit 1 ;;
esac
: "${AGENT_TEMPDIRECTORY:?Missing Agent temporary directory}"
: "${BAZEL_REMOTE_CACHE_URL:?Missing authorized remote cache URL}"
: "${CLIENT_CERT_ARCHIVE:?Missing cache certificate Secure File}"
: "${NATIVE_RESULTS_DIR:?Missing native build artifact directory}"

for tool in git curl tar python3 sha256sum cmp awk nproc df; do
    command -v "$tool" >/dev/null || { echo "Missing Agent tool: $tool" >&2; exit 1; }
done
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
    echo "Native builds require a Linux x86_64 Agent" >&2
    exit 1
fi
if [[ ! "$BAZEL_REMOTE_CACHE_URL" =~ ^grpcs://[^/:[:space:]]+:[0-9]+(/[^[:space:]]*)?$ ]]; then
    echo "The authorized cache URL must use grpcs:// with a host and port" >&2
    exit 1
fi
available_gib="$(df -Pk "$AGENT_TEMPDIRECTORY" | awk 'NR == 2 {print int($4 / 1048576)}')"
memory_gib="$(awk '/MemAvailable:/ {print int($2 / 1048576)}' /proc/meminfo)"
if (( available_gib < required_gib || memory_gib < 12 )); then
    echo "Insufficient Agent resources: disk=${available_gib}GiB memory=${memory_gib}GiB" >&2
    exit 1
fi
jobs="$(nproc)"
(( jobs <= 16 )) || jobs=16
(( jobs <= memory_gib / 4 )) || jobs=$((memory_gib / 4))
echo "Native target=$target jobs=$jobs available_disk=${available_gib}GiB"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_sha=63a88233694c85056096bc34843af39c5cedfcc6
run_root="$(mktemp -d "$AGENT_TEMPDIRECTORY/songqing-native.XXXXXXXX")"
# Budget for Bazel's hash and seven-digit sandbox IDs in Speakup's 256-byte buffer.
sandbox_root="$(mktemp -d /tmp/XXXXXX)"
cert_dir="$run_root/certs"
remote_rc="$run_root/remote.bazelrc"
cleanup() {
    rm -f -- "$cert_dir/server-ca.crt" "$cert_dir/writer-client.crt" "$cert_dir/writer-client.key" "$remote_rc"
    rm -rf -- "$sandbox_root"
}
trap cleanup EXIT
mkdir -p "$cert_dir" "$NATIVE_RESULTS_DIR" "$run_root/home/.cargo" "$run_root/tmp"
export HOME="$run_root/home"
export CARGO_HOME="$HOME/.cargo"
export GIT_CONFIG_GLOBAL="$HOME/.gitconfig"
export GIT_CONFIG_NOSYSTEM=1
export CARGO_NET_GIT_FETCH_WITH_CLI=true
export TMPDIR="$run_root/tmp"
printf '' > "$GIT_CONFIG_GLOBAL"
printf '[net]\ngit-fetch-with-cli = true\n' > "$CARGO_HOME/config.toml"

tar -xzf "$CLIENT_CERT_ARCHIVE" -C "$cert_dir"
for cert_file in server-ca.crt writer-client.crt writer-client.key; do
    if [[ ! -f "$cert_dir/$cert_file" || ! -s "$cert_dir/$cert_file" || -L "$cert_dir/$cert_file" ]]; then
        echo "Missing, empty or symlinked cache credential: $cert_file" >&2
        exit 1
    fi
done
chmod 0600 "$cert_dir/writer-client.key"
cat > "$remote_rc" <<EOF
build --remote_cache=$BAZEL_REMOTE_CACHE_URL
build --remote_instance_name=sonic-buildimage/e2e/trixie-amd64
build --remote_cache_async=false
build --remote_verify_downloads=true
build --remote_download_outputs=all
build --remote_timeout=120
build --remote_retries=5
build --tls_certificate=$cert_dir/server-ca.crt
build --tls_client_certificate=$cert_dir/writer-client.crt
build --tls_client_key=$cert_dir/writer-client.key
EOF

git init -q "$run_root/source"
cd "$run_root/source"
git remote add origin https://github.com/songqing-sq/sonic-buildimage.git
git fetch --depth=1 origin "$source_sha"
git checkout -q --detach FETCH_HEAD
test "$(git rev-parse HEAD)" = "$source_sha"
git submodule update --init --recursive --jobs=8
git submodule status --recursive > "$NATIVE_RESULTS_DIR/submodules.txt"
if grep -q '^[+-U]' "$NATIVE_RESULTS_DIR/submodules.txt"; then
    echo "A recursive submodule does not match its pinned revision" >&2
    exit 1
fi
git apply --check "$script_dir/songqing-inputs.patch"
git apply "$script_dir/songqing-inputs.patch"
sha256sum src/sonic-dash-ha/Cargo.{toml,lock} src/sonic-swss/Cargo.{toml,lock} \
    src/p4lang/p4c-native-bazel.patch \
    > "$run_root/source-inputs.sha256"

bazel_version="$(tr -d '\r\n' < .bazelversion)"
if [[ "$bazel_version" != 8.6.0 ]]; then
    echo "Unexpected native Bazel version: $bazel_version" >&2
    exit 1
fi
bazel_url="https://releases.bazel.build/$bazel_version/release/bazel-$bazel_version-linux-x86_64"
curl -fsSL --retry 2 --connect-timeout 15 --max-time 180 "$bazel_url" -o "$run_root/bazel"
curl -fsSL --retry 2 --connect-timeout 15 --max-time 60 "$bazel_url.sha256" -o "$run_root/bazel.sha256"
bazel_sha="$(awk 'NR == 1 {print $1}' "$run_root/bazel.sha256")"
[[ "$bazel_sha" =~ ^[[:xdigit:]]{64}$ ]] || { echo "Invalid Bazel release checksum" >&2; exit 1; }
printf '%s  %s\n' "$bazel_sha" "$run_root/bazel" | sha256sum --check
chmod 0700 "$run_root/bazel"
patch_sha="$(sha256sum "$script_dir/songqing-inputs.patch" | awk '{print $1}')"
control_sha="$(git -C "$script_dir/../../.." rev-parse HEAD)"
python3 - "$source_sha" "$control_sha" "$patch_sha" "$bazel_version" "$target" <<'PY' > "$NATIVE_RESULTS_DIR/provenance.json"
import json
import sys

print(json.dumps(dict(zip(
    ["source_commit", "ci_commit", "input_patch_sha256", "bazel_version", "target"],
    sys.argv[1:],
)), indent=2))
PY

build_flags=(
    "--sandbox_base=$sandbox_root"
    --experimental_scale_timeouts=4
    "--jobs=$jobs"
    "--local_resources=cpu=$jobs"
    "--repository_cache=$run_root/repository-cache"
    --disk_cache=
    "--repo_env=HOME=$HOME"
    "--repo_env=CARGO_HOME=$CARGO_HOME"
    "--repo_env=GIT_CONFIG_GLOBAL=$GIT_CONFIG_GLOBAL"
    --repo_env=GIT_CONFIG_NOSYSTEM=1
    --repo_env=CARGO_NET_GIT_FETCH_WITH_CLI=true
)
build_pass() {
    local pass="$1" accept_cached="$2" upload_results="$3"
    local started=$SECONDS
    echo "Native Bazel pass: $pass"
    "$run_root/bazel" --batch --nosystem_rc --nohome_rc \
        "--bazelrc=$remote_rc" \
        "--output_user_root=$run_root/$pass-root" \
        "--output_base=$run_root/$pass-output" \
        build "$label" "${build_flags[@]}" \
        "--remote_accept_cached=$accept_cached" \
        "--remote_upload_local_results=$upload_results" \
        "--build_event_json_file=$run_root/$pass.bep.json"
    local command_seconds=$(( SECONDS - started ))
    echo "Bazel invocation wall seconds: $command_seconds"
    printf '{"bazel_invocation_wall_seconds": %d}\n' "$command_seconds" \
        > "$NATIVE_RESULTS_DIR/$pass-command-timing.json"
    sha256sum --check "$run_root/source-inputs.sha256"
}

if [[ "$target" == docker-sysmgr ]]; then
    build_pass warm true true
    python3 "$script_dir/verify_songqing.py" warm "$run_root/warm.bep.json" \
        "$NATIVE_RESULTS_DIR/cache-validation.json"
    python3 "$script_dir/verify_songqing.py" oci \
        bazel-bin/dockers/docker-sysmgr/docker-sysmgr "$NATIVE_RESULTS_DIR/image-validation.json"
    tar -czhf "$NATIVE_RESULTS_DIR/docker-sysmgr.oci.tar.gz" \
        -C bazel-bin/dockers/docker-sysmgr/docker-sysmgr .
else
    build_pass vs true true
    python3 "$script_dir/verify_songqing.py" installer bazel-bin/platform/vs/sonic-vs.bin \
        bazel-bin/platform/vs/fs.zip "$NATIVE_RESULTS_DIR/installer-validation.json"
    python3 "$script_dir/verify_songqing.py" warm "$run_root/vs.bep.json" \
        "$NATIVE_RESULTS_DIR/cache-statistics.json"
    cp --reflink=auto bazel-bin/platform/vs/sonic-vs.bin "$NATIVE_RESULTS_DIR/sonic-vs.bin"
fi
echo "PASS: songqing native $target build and artifact verification"
