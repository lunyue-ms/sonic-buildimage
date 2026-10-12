# Songqing native Bazel CI

Pipeline 3430 uses `.azure-pipelines/azure-pipelines-bazel-cache.yml`.
All triggers remain disabled. The new `nativeTarget` parameter defaults to
`none`, preserving the existing `cacheOnly` and Make `BuildVS` modes.

For native builds, set `cacheOnly=false` and select:

| `nativeTarget` | Build and proof |
| --- | --- |
| `docker-sysmgr` | Compile the native OCI target and upload synchronously, then use fresh output roots with disk cache disabled to require matching C++ action remote hits and identical, checksum-verified OCI output. |
| `sonic-vs-bin` | Build the full VS/amd64 installer with remote reads and writes, verify its payload and ZIP, and publish the installer plus cache statistics. |

The YAML repository is the CI control plane, not the product source. Each native
job fetches public `songqing-sq/sonic-buildimage` commit
`63a88233694c85056096bc34843af39c5cedfcc6`, initializes pinned recursive
submodules, and uses that source's Bazel **8.6.0**. It does not invoke Make or the
legacy `azure-pipelines-build.yml` template. The existing `buildimage` resource
ref is retained for the unchanged legacy configuration.

`tools/bazel/ci/songqing-inputs.patch` contains the locally validated Monit m4
tool dependency and pins Rust `swss-common` to the locally tested commit
`60a1716be38d648c634ebffab547bbd5a67c8ef8`. Both Cargo manifests and lockfiles
are pinned so `master` cannot change between cache passes. Their checksums are
checked after each build. Artifacts record source, control and patch revisions.

The native job reuses `SONiC-Bazel-Cache`, `BAZEL_REMOTE_CACHE_URL`,
`sonic-bazel-cache-ci-certs.tar.gz`, and instance
`sonic-buildimage/e2e/trixie-amd64`. Certificates and the generated RC live in
a private Agent temporary directory, never in the repository or published
artifacts. They are removed when the script exits. No remote executor is used.
The native workspace RC remains enabled; only unrelated system/home RCs are
disabled. Git/Cargo use an isolated HOME and Git CLI; the kernel uses a short
sandbox path and the locally validated timeout scale.

## Run

After explicitly authorizing publication and pushing the test branch, either
select it in **Run pipeline**, or queue the same run with an authenticated CLI:

```bash
az pipelines run \
  --organization https://dev.azure.com/mssonic --project build --id 3430 \
  --branch lunyue/songqing-bazel-cache-ci \
  --parameters cacheOnly=false nativeTarget=docker-sysmgr
```

After the Docker/cache gate succeeds, select `nativeTarget=sonic-vs-bin`.
Prefer `--commit-id <published-CI-commit>` to pin the queued YAML revision.
Do not publish or queue additional runs without authorization.

The Docker artifact is a self-contained OCI archive, not a Docker-save archive.
Raw execution logs, connection RCs and credentials are not published; filtered
cache evidence and verified outputs are. A VS run publishes `sonic-vs.bin`, not
the traditional Make-generated `sonic-vs.img.gz`.

`DockerOverlay2Store` has a `local` execution requirement and cannot remotely
cache. Its local assembly is expected even on a warmed server. Full-build
success and cache hits do not establish VM boot, container runtime health,
unit/network tests, ARM or other ASIC support.

## Local checks

```bash
bash -n tools/bazel/ci/songqing_native.sh
python3 -m unittest discover -s tools/bazel/ci -p 'test_*.py'
```

Do not run the build script locally with CI writer credentials. It uploads
outputs when invoked and is intended for the authorized Agent job.
