#!/bin/bash
set -euo pipefail

results_dir="${1:?Expected results directory}"
if [[ ! -s /etc/sonic-bazel-remote.bazelrc || ! -w /bazel_cache ]]; then
    echo "Run inside sonic-slave with the remote RC and a writable /bazel_cache" >&2
    exit 1
fi
if [[ "$(bazel --version)" != "bazel $(tr -d '\r\n' < .bazelversion)" ]]; then
    echo "Bazel must match the checkout's .bazelversion" >&2
    exit 1
fi
work_root="$(mktemp -d /bazel_cache/remote-cache-proof.XXXXXXXX)"
sha256sum target/docker-config-engine-trixie.gz > "$work_root/base.sha256"
targets=(
    //dockers/docker-sysmgr:docker-sysmgr.gz
    //dockers/docker-sysmgr:docker-sysmgr-dbg.gz
)
for mode in write read; do
    accept_cached=false
    upload_results=true
    if [[ "$mode" == read ]]; then
        accept_cached=true
        upload_results=false
    fi
    mkdir "$work_root/$mode"
    startup_flags=(
        --batch "--output_user_root=$work_root/$mode-root"
        "--output_base=$work_root/$mode-output"
    )
    cache_flags=(
        --disk_cache= --remote_executor=
        "--remote_accept_cached=$accept_cached"
        "--remote_upload_local_results=$upload_results"
        --remote_cache_async=false --remote_verify_downloads=true
        --remote_download_outputs=all
    )
    bazel "${startup_flags[@]}" build "${targets[@]}" "${cache_flags[@]}" \
        "--execution_log_json_file=$work_root/$mode/execution.json" \
        "--build_event_json_file=$work_root/$mode/events.json"
    for target in "${targets[@]}"; do
        out="$(bazel "${startup_flags[@]}" cquery --output=files "$target" "${cache_flags[@]}")"
        if [[ ! -f "$out" || "$out" != *.gz ]]; then
            echo "Expected one archive output for $target" >&2
            exit 1
        fi
        cp "$out" "$work_root/$mode/${target##*:}"
    done
done
sha256sum --check "$work_root/base.sha256"
python3 tools/bazel/ci/verify_remote_cache.py cache "$work_root" \
    target/docker-sysmgr.manifest.json target/docker-sysmgr-dbg.manifest.json \
    "$results_dir/cache-validation.json"
cp "$work_root/read/docker-sysmgr.gz" "$work_root/read/docker-sysmgr-dbg.gz" "$results_dir/"
cp target/docker-sysmgr.manifest.json target/docker-sysmgr-dbg.manifest.json "$results_dir/"
