# SONiC Buildimage Guide
## Overview
SONiC buildimage is a *GNU make* based environment for build process automation.
It consists of two main parts:
 * Backend - collection of makefiles and other helpers that define generic target groups, used by recipes
 * Frontend - collection of recipes, that define metadata for each build target

## Structure
File structure of SONiC Buildimage is as follows:  
```
sonic-buildimage/  
  Makefile  
  slave.mk  
  sonic-slave/  
    Dockerfile  
  rules/  
    config  
    functions
    recipe1.mk
    ..
  dockers/  
    docker1/  
      Dockerfile.template    
    ..  
  src/  
    submodule1/    
    ..  
    package1/  
      Makefile  
    ..  
  platform/  
    vendor1/  
    ..  
  target/  
    debs/  
    python-wheels/  
```
### Backend  
**Makefile**, **slave.mk** and **sonic-slave/Dockerfile** are the backend of buildimage.  
*slave.mk* is the actual makefile. It defines a set of rules for *target groups* (more on that later).
You can find a make rule for every target that is defined in recipe there.  
*Makefile* is a wrapper over sonic-slave docker image.  

Every part of build is executed in a docker container called sonic-slave, specifically crafted for this environment.
If build is started for the first time on a particular host, a new sonic-slave image will be built from *sonic-slave/Dockerfile* on the machine.
It might take some time, so be patient.
After that all subsequent make commands will be executed inside this container.
*Makefile* takes every target that is passed to make command and delegates it as an entry point to a container,
making process of running container transparent.  
  
### Frontend  
**rules/** has a collection of recipes for platform independent targets.
Every recipe is a file that describes a metadata of a specific target, that is needed for its build.  
You might find **rules/config** very useful, as it is a configuration file for a build system, which enables/disables some tweaks.  
**dockers/** directory is a place where you can find Dockerfiles for generic docker images.  
**src/** is a place where a source code for generic packages goes.
It has both submodules (simple case, just run dpkg-buildpackage to build),
and directories with more complicated components, that provide their own Makefiles.
**platform/** contains all vendor-specific recipes, submodules etc.  
Every **platform/[VENDOR]/** directory is a derived part of buildimage frontend, that defines rules and targets for a concrete vendor.  

### Build output
**target/** is basically a build output. You can find all build artifacts there.  

## Recipes and target groups
Now let's go over a definition of recipes and target groups.  
**Recipe** is a small makefile that defines a target and set of variables for building it.
If you want to add a new target to buildimage (.deb package or docker image), you have to create a recipe for this target.  
**Target group** is a set of targets that are built according to the same rules.
Every recipe sets a target group to which this target belongs.  

### Recipe example
Lets take a recipe for swsscommon as an example:  
```make
# libswsscommon package

LIBSWSSCOMMON = libswsscommon_1.0.0_amd64.deb
$(LIBSWSSCOMMON)_SRC_PATH = $(SRC_PATH)/sonic-swss-common
$(LIBSWSSCOMMON)_DEPENDS += $(LIBHIREDIS_DEV) $(LIBNL3_DEV) $(LIBNL_GENL3_DEV) \
                            $(LIBNL_ROUTE3_DEV) $(LIBNL_NF3_DEV) \
			    $(LIBNL_CLI_DEV)
$(LIBSWSSCOMMON)_RDEPENDS += $(LIBHIREDIS) $(LIBNL3) $(LIBNL_GENL3) \
                             $(LIBNL_ROUTE3) $(LIBNL_NF3) $(LIBNL_CLI)
SONIC_DPKG_DEBS += $(LIBSWSSCOMMON)

LIBSWSSCOMMON_DEV = libswsscommon-dev_1.0.0_amd64.deb
$(eval $(call add_derived_package,$(LIBSWSSCOMMON),$(LIBSWSSCOMMON_DEV)))
```
First we define our package swsscommon.  
Then we specify **SRC_PATH** (path to sources),
**DEPENDS** (build dependencies),
and **RDEPENDS** (runtime dependencies for docker installation).  
Then we add our target to SONIC_DPKG_DEBS target group.  
At the end we define a dev package for swsscommon and make it derived from main one.
Using **add_derived_package** just makes a deep copy of package's metadata, so that we don't have to repeat ourselves.  

### Target groups
**SONIC_DPKG_DEBS**  
Main target group for building .deb packages.
Define:  
```make
SOME_NEW_DEB = some_new_deb.deb # name of your package
$(SOME_NEW_DEB)_SRC_PATH = $(SRC_PATH)/project_name # path to directory with sources
$(SOME_NEW_DEB)_DEPENDS = $(SOME_OTHER_DEB1) $(SOME_OTHER_DEB2) ... # build dependencies
$(SOME_NEW_DEB)_RDEPENDS = $(SOME_OTHER_DEB1) $(SOME_OTHER_DEB2) ... # runtime dependencies
SONIC_DPKG_DEBS += $(SOME_NEW_DEB) # add package to this target group
```

**SONIC_PYTHON_STDEB_DEBS**  
Same as above, but instead of building package using dpkg-buildpackage it executes `python setup.py --command-packages=stdeb.command bdist_deb`.
Define:  
```make
SOME_NEW_DEB = some_new_deb.deb # name of your package
$(SOME_NEW_DEB)_SRC_PATH = $(SRC_PATH)/project_name # path to directory with sources
$(SOME_NEW_DEB)_DEPENDS = $(SOME_OTHER_DEB1) $(SOME_OTHER_DEB2) ... # build dependencies
$(SOME_NEW_DEB)_RDEPENDS = $(SOME_OTHER_DEB1) $(SOME_OTHER_DEB2) ... # runtime dependencies
SONIC_PYTHON_STDEB_DEBS += $(SOME_NEW_DEB) # add package to this target group
```

**SONIC_MAKE_DEBS**  
This is a bit more flexible case.
If you have to do some specific type of build or apply patches prior to build, just define your own Makefile and add it to buildimage.
Define:
```make
SOME_NEW_DEB = some_new_deb.deb # name of your package
$(SOME_NEW_DEB)_SRC_PATH = $(SRC_PATH)/project_name # path to directory with sources
$(SOME_NEW_DEB)_DEPENDS = $(SOME_OTHER_DEB1) $(SOME_OTHER_DEB2) ... # build dependencies
$(SOME_NEW_DEB)_RDEPENDS = $(SOME_OTHER_DEB1) $(SOME_OTHER_DEB2) ... # runtime dependencies
SONIC_MAKE_DEBS += $(SOME_NEW_DEB) # add package to this target group
```

If some packages have to be built locally due to some legal issues or they are already prebuilt and available online, you might find next four target groups useful.  

**SONIC_COPY_DEBS**  
Those packages will be just copied from specified location on your machine.
Define:  
```make
SOME_NEW_DEB = some_new_deb.deb # name of your package
$(SOME_NEW_DEB)_PATH = path/to/some_new_deb.deb # path to file
SONIC_COPY_DEBS += $(SOME_NEW_DEB) # add package to this target group
```

**SONIC_COPY_FILES**  
Same as above, but applicable for regular files. Use when you need some regular files for installation in docker container.
Define:  
```make
SOME_NEW_FILE = some_new_file # name of your file
$(SOME_NEW_FILE)_PATH = path/to/some_new_file # path to file
SONIC_COPY_FILES += $(SOME_NEW_FILE) # add file to this target group
```

**SONIC_ONLINE_DEBS**  
Target group for debian packages that should be fetched from an online source.
Define:  
```make
SOME_NEW_DEB = some_new_deb.deb # name of your package
$(SOME_NEW_DEB)_URL = https://url/to/this/deb.deb # path to file # URL for downloading
SONIC_ONLINE_DEBS += $(SOME_NEW_DEB) # add file to this target group
```

**SONIC_ONLINE_FILES**  
Target group for regular files that should be fetched from an online source.
Define:  
```make
SOME_NEW_FILE = some_new_file # name of your file
$(SOME_NEW_FILE)_URL = https://url/to/this/file # URL for downloading
SONIC_ONLINE_FILES += $(SOME_NEW_FILE) # add file to this target group
```

Docker images also have their target groups.  
**SONIC_SIMPLE_DOCKER_IMAGES**  
As you see from a name of the group, it is intended to build a docker image from a regular Dockerfile.
Define:  
```make
SOME_DOCKER = some_docker.gz # name of your docker
$(SOME_DOCKER)_PATH = path/to/your/docker # path to your Dockerfile
SONIC_SIMPLE_DOCKER_IMAGES += $(SOME_DOCKER) # add docker to this group
```

**SONIC_DOCKER_IMAGES**  
This one is a bit more sophisticated. You can define debian packages from buildimage that will be installed to it, and corresponding Dockerfile will be dynamically generated from a template.
Define:  
```make
SOME_DOCKER = some_docker.gz # name of your docker
$(SOME_DOCKER)_PATH = path/to/your/docker # path to your Dockerfile
$(SOME_DOCKER)_DEPENDS += $(SOME_DEB1) $(SOME_DEB2) # .deb packages to install into image
$(SOME_DOCKER)_PYTHON_WHEELS += $(SOME_WHL1) $(SOME_WHL2) # python wheels to install into image
$(SOME_DOCKER)_LOAD_DOCKERS += $(SOME_OTHER_DOCkER) # docker image from which this one is built
SONIC_DOCKER_IMAGES += $(SOME_DOCKER) # add docker to this group
```

**SONIC_BAZEL_DOCKER_IMAGES**
Target group for docker images that can be built with [Bazel](https://bazel.build/) instead of the legacy Make-based flow.

Each component can opt-into this mechanism by joining the group, and specifying how Bazel-ready it is:

```make
$(SOME_DOCKER)_BAZEL_READINESS = experimental # If consumers can try out the Bazel build, but shouldn't trust it fully.
$(SOME_DOCKER)_BAZEL_READINESS = stable # If consumers should use the Bazel build as the canonical, and only fall back if really necessary.
SONIC_BAZEL_DOCKER_IMAGES += $(SOME_DOCKER) # add docker to this target group
```

A docker outside the group is never built with Bazel. A docker in it must declare a readiness level, and a readiness level declared outside it is a build error.

For now, the readiness level is a maintainer's judgement.

- `experimental` means a maintainer has opened the Bazel build for the community to try, but still considers the Make-based build the canonical one.
- `stable` means a maintainer vouches for the image in production terms, and considers the Bazel build canonical for that component. It deliberately does **not** mean the Bazel image is byte-identical to the Make-built one: a Bazel image may legitimately differ and still be the better artifact.

The image is still registered in `SONIC_DOCKER_IMAGES` / `SONIC_INSTALL_DOCKER_IMAGES`, and still carries `_PATH`, `_VERSION` and `_PACKAGE_NAME`, so it is installed and listed in the sonic-package-manager catalog exactly as a Make-built one. Irrelevant attributes such as `_DEPENDS` and `_LOAD_DOCKERS` are declared unconditionally too, and are simply left unread whenever the Bazel path is selected.

Debug images inherit their parent's readiness.

To make a target build with Bazel, define its readiness, as well as a the base image it should have:

```make
SOME_DOCKER = some_docker.gz # name of your docker (must match dockers/<name>/BUILD.bazel)
$(SOME_DOCKER)_BAZEL_BASE += $(SOME_BASE_DOCKER) # base docker(s) the Bazel build depends on
$(SOME_DOCKER)_BAZEL_READINESS = experimental # how ready the Bazel build is
SONIC_BAZEL_DOCKER_IMAGES += $(SOME_DOCKER) # add docker to this target group
```

When building, you can use the `BAZEL_MIN_READINESS` configuration knob (defined in **rules/config**):

| `BAZEL_MIN_READINESS` | Components built with Bazel |
| --- | --- |
| `bazel_disabled` | none |
| `experimental` | those declaring `experimental` or `stable` |
| `stable` | those declaring `stable` |

### Bazel manifests and remote cache validation

The Bazel Make recipes render the same manifest template and optional partial
manifest as the legacy Docker recipes. They write independent
`target/<docker>.manifest.json` and `target/<docker>-dbg.manifest.json` inputs.
The OCI label conversion is a declared Bazel action: version, service ordering,
container options, CLI plugins and debug build metadata are not hardcoded.
When invoking Bazel directly, first build the Make base archive and both
manifest targets.

`SONIC_BAZEL_REMOTE_CACHE_CONFIG=/absolute/path/to/cache.bazelrc` optionally mounts
a readable host RC at `/etc/sonic-bazel-remote.bazelrc`, read-only. The root
workspace `.bazelrc` imports it, not the slave system RC: nested workspaces such
as `sonic-p4rt/sonic-pins` pin older Bazel versions that reject root-workspace
remote options. Mount any certificate files separately with
`DOCKER_BUILDER_USER_MOUNT`; never put credentials in the checkout. A configured
remote RC with a missing file or unusable per-user cache directory fails rather
than silently building without the requested cache. Unconfigured builds keep
their existing local-cache behavior.

`.azure-pipelines/azure-pipelines-bazel-cache.yml` is a **manual-only** validation
pipeline. It builds the selected checkout and its recorded submodules, without
requiring a historical PR commit to remain in its ancestry. It reuses the
`SONiC-Bazel-Cache` variable group,
`BAZEL_REMOTE_CACHE_URL`, and the existing
`sonic-bazel-cache-ci-certs.tar.gz` Secure File. No cache service, TLS or
authorization changes are required.

Cleanup restores owner write permission on directories in the private temporary
tree before removing it, since hermetic SDK directories can be read-only. It
does not follow symlinks and preserves the original build exit status.

The job uses `BAZEL_MIN_READINESS=experimental`, trixie/amd64, and
`SONIC_DPKG_CACHE_METHOD=none`. It builds the config-engine base once with Make,
then runs real sysmgr normal/debug Bazel builds in two fresh output roots:
the write pass disables remote reads and uploads synchronously; the read pass
disables uploads and reads the remote cache. Disk caching and remote execution
are disabled. The gate requires matching action digests and genuine remote
hits for **every recorded cacheable sysmgr CppCompile**, complete manifest-label
equality, identical cold/warm archives, and successful BEP timing events.
Repository downloads may be shared; they are not counted as compile-cache hits.
Execution logs are streamed once per pass and are not published.

After the cache gate, the job builds `target/sonic-vs.img.gz` through the normal
Make path, retaining the base archive already produced in this job with
`--old-file` forwarded through `SONIC_BUILD_VARS`. This prevents the phony
`docker-start` prerequisite from rebuilding a time-varying base between proof
and integration; it does not reuse a dpkg/image cache. The job checks gzip
integrity and requires its sysmgr archive to match the validated warm result.
**The current migration selects sysmgr for Bazel**; the
base, other containers and image assembly remain Make-built. Published outputs
are selected images, generated manifests, source/submodule provenance and a
sanitized cache/timing report. This is build integration evidence, not boot,
runtime, upgrade, FIPS or cross-Agent cache acceptance. Publishing the branch
and queuing this pipeline require separate authorization.

The job has a 600-minute budget, matching the existing uncached image comparison
jobs: the legacy Make kernel, other containers and installer are not accelerated
by sysmgr's Bazel cache. A 10-minute cancellation grace allows cleanup and
publication of existing sanitized evidence after cancellation or timeout.
Publication still requires the results directory to have been created and does
not turn an interrupted VS integration into a passing result.

Focused local regressions (without a cache connection or full image build):

```sh
python3 -m pytest -q tools/bazel/ci/remote_cache_test.py \
    tools/bazel/oci/manifest_to_labels_test.py \
    tools/bazel/oci/docker_archive_to_oci_layout_test.py
shellcheck tools/bazel/ci/configure_remote_cache.sh \
    tools/bazel/ci/remote_cache_ci.sh tools/bazel/ci/validate_remote_cache.sh
```

### Bazel VS nightly

`.azure-pipelines/azure-pipelines-bazel-nightly.yml` follows the `BuildVS` and
Elastictest path in `azure-pipelines.yml`. It schedules master daily at 08:00 UTC,
disables push/PR triggers, and must not be registered as a required PR check.
The initial scope is a normal VS/amd64/Trixie image, including the existing
multi-ASIC build option and `ENABLE_FIPS=y`. ASAN, other build platforms and the
remaining PR test topologies are not covered by this initial nightly.
The selected source must contain the migrated Bazel rules; enabling a flag
does not migrate an older Make-only checkout.

The normal `CACHE_MODE=rcache` DPKG cache remains enabled. A private, writable
Bazel cache and mTLS configuration are created outside the workspace by
`tools/bazel/ci/configure_remote_cache.sh`. Only the root workspace imports the
remote RC, so nested projects using older Bazel versions retain their existing
behavior. Local disk caching is allowed; remote misses are normal and do not
fail the nightly. The shared setup is also used by the stricter manual cache
proof, whose cold-write/warm-read policy remains separate.

The shared build template's `postCheckoutSteps` hook runs after the requested
commit is checked out and before `make configure`. Existing `preSteps` retain
their original order. Both hook defaults are empty. `triggerArtifactPublish`
defaults to true for existing callers; the nightly sets it false to avoid
queuing the additional external `publish-artifacts` pipeline. Standard ADO
artifacts, including `sonic-buildimage.vs`, are still archived.

After the build, the nightly requires the Make-generated sysmgr manifest,
a successful Bazel build in its Make log, matching complete OCI manifest
metadata, and a valid VS gzip archive. `bazel-nightly.json` records the source
commit, Bazel version, build/pipeline IDs, sysmgr archive/config digests and
the final VS image digest. Process/cache counts describe only the last
sysmgr build recorded in that log, not all VS actions or C++ compilations.
Remote-cache warnings are surfaced as pipeline warnings. These diagnostics
do not replace the manual two-root remote-hit proof or prove the contents of
a booted DUT.

The test stage reuses sonic-mgmt's existing Elastictest lifecycle template
for the t0 and t1-lag PR test sets. Each topology requests one worker and one
plan, pins the test-code commit from the repository resource checkout, and
requires `SUCCESS`. Both the KVM build ID and pipeline ID identify this exact
nightly's standard VS artifact; no latest-master image is requested.
Scheduled ADO execution intentionally uses the Elastictest `PR` plan type for
its KVM pools, not a persistent `NIGHTLY` testbed schedule.

Registering the pipeline and authorizing the existing variable groups,
Secure File, GitHub endpoint and Elastictest service are separate operational
steps. Cache-writer credentials must only be available to trusted CI.
The shared sonic-mgmt template currently recognizes canonical public buildimage
repository names when downloading test definitions; manually running it from
a fork requires checking that routing before queueing, not overriding the
built-in source identity. No pipeline registration or execution is performed
by adding this YAML.

Make/Bazel layout and permission comparisons, dependency-version drift gates,
additional architectures and live full-pipeline acceptance remain separate
follow-up work. The local regressions above also cover the nightly wiring,
credential setup and sanitized evidence generation; they do not compile an
ADO pipeline or execute a VS/FIPS/Elastictest run.

## Tips & Tricks
Although every target is built inside a sonic-slave container, which exits at the end of build, you can enter bash of sonic-slave using this command:
```
$ make sonic-slave-bash
```
It is very useful for debugging when you add a new target and facing some troubles.

sonic-slave environment is built only once, but if sonic-slave/Dockerfile was updated, you can rebuild it with this command:
```
$ make sonic-slave-build
```

One can print out all available targets by executing the following command:
```
$ make list
```

All target groups are used by one or another recipe, so use those recipes as a reference when adding new ones.

## Build debug dockers and debug SONiC installer image:
Using 'INSTALL_DEBUG_TOOLS=y' builds the debug image.

For example:
`INSTALL_DEBUG_TOOLS=y make target/sonic-broadcom.bin` 

* Builds debug docker images.
* Debug images carry a suffix of "-dbg"
    * e.g. target/docker-orchagent-dbg.gz
* target/sonic-broadcom.bin is built using debug docker images
    * Selective sources are archived and available under /src
    * An empty /debug dir is created for use during debug session.
    * All debug dockers are mounted with /src:ro and /debug:rw
    * Login banner will briefly describe these features.
  

_Note: target/sonic-broadcom.bin name is the same irrespective of built using debug or non-debug dockers._

_Recommend: Rename image built using INSTALL_DEBUG_TOOLS=y to mark it explicit. May be `mv target/sonic-broadcom.bin target/sonic-broadcom-dbg.bin`_

### Debug dockers
* Built with all available debug symbols.
* Installed with many basic packages that would be required for debugging
    * gdb
    * gdbserver
    * vim
    * strace
    * openssh-client
    * sshpass
* Loadable into any environment that supports docker
* Outside SONiC image, you may run the docker with `--entrypoint=/bin/bash` 
* Use -v to map any of your host directories
* To debug a core file in non-SONiC environment that supports docker
    * `docker load -i docker-<name>-dbg.gz`
    * copy your unzipped core file into ~/debug
    * `docker run -it --entrypoint=/bin/bash -v ~/debug:/debug <image id>`
    * `gdb /usr/bin/<your binary> -c /debug/<your core>`
    
### Debug SONiC image

* Install this image into the switch that supports this image.
	* For platform independent binary, you may use a debug image for virtual switch
* Open the archive in /src, if you would need source code for debugging
* Every debug enabled docker is mounted with /src as read-only
* The host has /debug dir and it is mapped into every debuggable docker as /debug with read-write permission.
* To debug a core
    * Copy core into /debug of host and unzip it
        * Feel free to create & use sub-dirs as needed.
        * Entire /debug is mounted inside docker
    * Get into the docker (`docker -it exec <name> bash`)
    * `gdb /usr/bin/<binary> -c /debug/<core file path>`
    * Use set-directory in gdb to map appropriate source dir from under /src
    * You may set gdb logs to go into /debug
* For live debugging
    * Use this as a regular switch
    * Get into docker, and you may
        * start process under dbg
        * attach gdb to running process
    * Set required source dir from under /src as needed
    * May use /debug to record all gdb logs or any spew from debug session.
    
  ### To enhance debug dockers
  * Add to `<docker name>_DBG_IMAGE_PACKAGES`, additional debug tools that will be pre-installed during build.
	* e.g. `$(DOCKER_ORCHAGENT)_DBG_IMAGE_PACKAGES += perl`
	* Build will install these tools using "apt"
  * Add to `<docker name>_DBG_DEPENDS`, additional debug .deb packages that will be pre-installed during build.
	* e.g. `$(DOCKER_ORCHAGENT)_DBG_DEPENDS +=  $(SWSS_DBG) $(LIBSWSSCOMMON_DBG) $(LIBSAIREDIS_DBG)`
  * Add to `DBG_SRC_ARCHIVE`, the source dirs to archive
	* The source files (.c, .cpp, .h & .hpp) under this dir tree are archived.
	* e.g. rules/swss.mk has `DBG_SRC_ARCHIVE += swss`
