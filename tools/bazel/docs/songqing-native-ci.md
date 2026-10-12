# Songqing native Bazel cross-run cache benchmark

Pipeline 3430 uses `.azure-pipelines/azure-pipelines-bazel-cache.yml`.
This dedicated `lunyue/songqing-bazel-warm-cache-ci` branch defaults
`nativeTarget` to `docker-sysmgr`. Push and PR triggers are disabled.
Select `sonic-vs-bin` explicitly for the full VS build or `none` for the existing
`cacheOnly`/Make modes. The temporary branch/path-scoped GitHub trigger used for
authorized full VS validation has been disabled after the successful run. It
used pipeline 3430's existing connection without changing authentication or
permissions. The original `lunyue/songqing-bazel-cache-ci` branch remains unchanged.

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

The sandbox is an atomically allocated, mode-0700 `/tmp/XXXXXX` directory.
Speakup's host `makemapdata` tool builds input filenames in a 256-byte buffer.
In full VS run 1246066, the old `/tmp/sq.XXXXXXXX` prefix and sandbox ID 10565
produced a 258-byte `spk_priv_keyinfo.h` path; the tool silently truncated it and
the kernel action failed. The shorter prefix keeps that path at 253 bytes and
even a seven-digit sandbox ID at 255 bytes, leaving room for the NUL terminator.
An isolated regression with the actual kernel tool reproduces the old failure
and generates byte-identical headers at short, five-digit and seven-digit IDs
with the corrected prefix. This changes neither kernel source/configuration nor
timeouts, cache settings or certificate isolation.

## Verified full VS result

Run [1246182](https://dev.azure.com/mssonic/build/_build/results?buildId=1246182)
succeeded on 2026-10-12 (UTC), using control commit
`6bfeb154295e6ecd4fe5d28eb0286193797ff2c1` and Agent 33. The native job,
installer verification and artifact publication passed; legacy `BuildVS` was
skipped. Product source and the controlled patch match the recorded provenance.

| Measurement | Result |
| --- | --- |
| CI runtime, excluding queue | 10,973.094 seconds (3h 2m 53s) |
| Queue | 344.350 seconds (5m 44s) |
| Bazel reported elapsed | 10,731.572 seconds (2h 58m 52s, rounded) |
| Complete Bazel process | 10,737 seconds |
| BEP wall / analysis / execution | 10,728.942 / 1,135.331 / 9,573.616 seconds |
| Remote / sandbox / local / internal runners | 14,990 / 1,157 / 1 / 7,933 |
| Published installer size | 1,881,622,511 bytes |

Artifact `native-bazel-sonic-vs-bin` contains the installer and filtered
provenance, installer-validation, cache-statistics and command-timing evidence.
Installer payload SHA1, embedded ZIP equality and ZIP CRC passed. Its SHA256 is
`419452b4f7984a7fb1289f198d674110b8c9e4fe812586de12cadeff20f7890b`.

The remote hit fraction among execution spawns is 14,990 / 16,148 = 92.829%;
internal actions are not compile spawns. This is not a cost-weighted ratio:
the kernel action still executed locally and its progress exceeded 7,800 seconds.
The run used a different Agent from the failed full VS attempts, but still had
cold misses. It is the first successful full VS CI, not a second fully warmed VS
benchmark or a boot/runtime test.

## Run

For an explicitly selected Docker benchmark, choose this branch in **Run
pipeline** or use an authenticated CLI:

```bash
az pipelines run \
  --organization https://dev.azure.com/mssonic --project build --id 3430 \
  --branch lunyue/songqing-bazel-warm-cache-ci \
  --parameters cacheOnly=false nativeTarget=docker-sysmgr
```

For a manually queued full VS run, select `nativeTarget=sonic-vs-bin`.
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
