# Songqing native Bazel cross-run cache benchmark

Pipeline 3430 uses `.azure-pipelines/azure-pipelines-bazel-cache.yml`.
This dedicated `lunyue/songqing-bazel-warm-cache-ci` branch defaults
`nativeTarget` to `docker-sysmgr`. All triggers remain disabled. Select `none`
explicitly to use the existing `cacheOnly` or Make `BuildVS` modes. The original
`lunyue/songqing-bazel-cache-ci` branch remains unchanged.

For native builds, set `cacheOnly=false` and select:

| `nativeTarget` | Build and proof |
| --- | --- |
| `docker-sysmgr` | Build the native OCI target once with remote reads and writes enabled, fresh output roots and disk cache disabled; require remote hits from an earlier CI run and checksum-verify the OCI output. |
| `sonic-vs-bin` | Build the full VS/amd64 installer once with remote reads and writes, require remote hits through lightweight build-event metrics, verify its payload and ZIP, and publish the installer plus cache statistics. |

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

The controlled patch also fixes P4C's custom Yacc action to pass
`BisonToolchainInfo.bison_env` instead of the host shell environment. This
declares the M4/runfiles environment required by `rules_bison`, without
installing host M4 or changing parser/compiler versions. Full VS run 1245971
exposed the omission in `ir_generator_yacc`; an isolated local build reproduced
the exact error with unusable host M4, then passed with the toolchain environment.
The P4C patch checksum is checked after the build alongside the Cargo inputs.

There is no cold compilation or second build within this job. The isolated HOME,
source, output roots and repository cache are created afresh for each invocation;
CI pool allocation decides the Agent VM. The cache instance stays unchanged.
The warm build uses `--remote_accept_cached=true` and
`--remote_upload_local_results=true`, so misses execute locally and populate
the server.

`cache-validation.json` (Docker) or `cache-statistics.json` (VS) records Bazel
wall, analysis and execution times plus runner counts from Bazel 8.6.0's
build-event protocol. It requires a successful
finished event, positive remote hits and no disk-cache hits. The benchmark
does not generate the large per-spawn JSON logs whose repeated parsing took
about 40 minutes in run 1245568. It does not claim per-C++ action digest proof;
that remains the separate cold/write-read gate on the original branch.
`warm-command-timing.json` (Docker) or `vs-command-timing.json` (VS) also records
the complete Bazel process wall time, including startup and shutdown, at
one-second resolution. BEP timing excludes some startup/shutdown overhead;
keep these measurements distinct.

The VS target is `//platform/vs:sonic-vs-bin`: kernel, initrd, host rootfs,
30 OCI image targets (3 bases and 27 runtime images), Docker store, SquashFS
and ONIE installer. The first full VS CI can reuse cached common actions from
the Docker runs, but additional components may still compile locally. It is
not a fully warmed VS benchmark, and it does not boot or deploy the image.

The comparison baseline is run 1245568: the forced-local write build took
2976.141 seconds, while its same-job clean-output remote read took 134.238
seconds. A new CI also pays for source/dependency downloads and analysis;
report its actual Bazel and total CI times separately rather than assuming
the same-job result predicts cross-VM performance.

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
  --branch lunyue/songqing-bazel-warm-cache-ci \
  --parameters cacheOnly=false nativeTarget=docker-sysmgr
```

After the Docker/cache gate succeeds, select `nativeTarget=sonic-vs-bin`.
Prefer `--commit-id <published-CI-commit>` to pin the queued YAML revision.
Do not publish or queue additional runs without authorization.

The Docker artifact is a self-contained OCI archive, not a Docker-save archive.
Raw execution/build-event logs, connection RCs and credentials are not
published; filtered cache evidence and verified outputs are. A VS run publishes
`sonic-vs.bin`, not the traditional Make-generated `sonic-vs.img.gz`.

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
