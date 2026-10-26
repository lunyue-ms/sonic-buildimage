#!/bin/bash
set -euo pipefail

config_dir="${1:?Expected a private configuration directory}"
: "${BAZEL_REMOTE_CACHE_URL:?}" "${BAZEL_REMOTE_INSTANCE_NAME:?}" "${CLIENT_CERT_ARCHIVE:?}"
if [[ ! "$BAZEL_REMOTE_CACHE_URL" =~ ^grpcs://[^/:[:space:]]+:[0-9]+(/[^[:space:]]*)?$ ]]; then
    echo "BAZEL_REMOTE_CACHE_URL must contain a grpcs:// host and port" >&2
    exit 1
fi
if [[ ! "$BAZEL_REMOTE_INSTANCE_NAME" =~ ^[a-zA-Z0-9._/-]+$ ]]; then
    echo "BAZEL_REMOTE_INSTANCE_NAME must be a nonempty instance path without whitespace" >&2
    exit 1
fi
if [[ "$config_dir" != /* || "$config_dir" == *[[:space:],]* ]]; then
    echo "The configuration directory must be absolute and contain no whitespace or commas" >&2
    exit 1
fi

umask 077
mkdir -m 0700 -- "$config_dir"
mkdir "$config_dir/certs" "$config_dir/cache"
members="$(tar -tzf "$CLIENT_CERT_ARCHIVE")"
for name in server-ca.crt writer-client.crt writer-client.key; do
    if ! member="$(printf '%s\n' "$members" | grep -xE "(\./)?${name//./\\.}")" ||
        [[ "$member" == *$'\n'* ]]; then
        echo "Expected one certificate archive member: $name" >&2
        exit 1
    fi
    tar -xOzf "$CLIENT_CERT_ARCHIVE" -- "$member" > "$config_dir/certs/$name"
    if [[ ! -s "$config_dir/certs/$name" ]]; then
        echo "Missing or empty certificate file: $name" >&2
        exit 1
    fi
done

cat > "$config_dir/remote.bazelrc" <<EOF
startup --batch
common --remote_cache=$BAZEL_REMOTE_CACHE_URL
common --remote_instance_name=$BAZEL_REMOTE_INSTANCE_NAME
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
EOF
