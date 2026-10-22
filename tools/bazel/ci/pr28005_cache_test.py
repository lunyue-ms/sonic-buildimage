import copy
import gzip
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import yaml

import verify_pr28005_cache as verify

ROOT = Path(__file__).resolve().parents[3]
ACTION = "a" * 64
COMPILE = {
    "mnemonic": "CppCompile",
    "runner": "processwrapper-sandbox",
    "remoteCacheable": True,
    "targetLabel": "@@sonic-sysmgr+//:sysmgr",
    "digest": {"hash": ACTION},
}


def write_records(path, records):
    path.write_text("".join(json.dumps(record, indent=2) + "\n" for record in records))


def write_archive(path, stem, manifest, architecture="amd64"):
    config = {
        "os": "linux",
        "architecture": architecture,
        "config": {"Labels": {"com.azure.sonic.manifest": json.dumps(manifest)}},
    }
    files = {
        "config.json": config,
        "manifest.json": [{"RepoTags": [stem + ":latest"], "Config": "config.json", "Layers": []}],
    }
    with tarfile.open(path, "w:gz") as archive:
        for name, value in files.items():
            data = json.dumps(value).encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


class CacheProofTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.normal = self.root / "normal.json"
        self.debug = self.root / "debug.json"
        manifest = {
            "package": {"name": "sysmgr", "version": "2.3.4+vendor"},
            "service": {"name": "sysmgr", "after": ["database"]},
            "container": {"volumes": ["/etc/sonic:/etc/sonic:ro"]},
            "cli": {"config": "custom.plugin"},
        }
        debug_manifest = copy.deepcopy(manifest)
        debug_manifest["package"]["version"] += ".dbg"
        self.normal.write_text(json.dumps(manifest))
        self.debug.write_text(json.dumps(debug_manifest))
        for mode in ("write", "read"):
            (self.root / mode).mkdir()
            record = dict(COMPILE)
            if mode == "read":
                record.update(runner="remote cache hit", cacheHit=True)
            write_records(self.root / mode / "execution.json", [record])
            events = [
                {"finished": {"overallSuccess": True}},
                {"buildMetrics": {"timingMetrics": {
                    "wallTimeInMs": "2500",
                    "analysisPhaseTimeInMs": "1000",
                    "executionPhaseTimeInMs": "1400",
                }}},
            ]
            (self.root / mode / "events.json").write_text(
                "".join(json.dumps(event) + "\n" for event in events)
            )
        for stem, expected in (
            ("docker-sysmgr", manifest), ("docker-sysmgr-dbg", debug_manifest)
        ):
            write_archive(self.root / "write" / (stem + ".gz"), stem, expected)
            shutil.copyfile(
                self.root / "write" / (stem + ".gz"), self.root / "read" / (stem + ".gz")
            )

    def validate(self):
        return verify.verify_cache(self.root, self.normal, self.debug)

    def test_complete_cache_and_integration_proof(self):
        report = self.validate()
        self.assertEqual(report["sysmgr_cpp_remote_hits"], 1)
        self.assertEqual(report["matching_action_samples"][0]["digest"], ACTION)
        self.assertEqual(report["write_timing"]["wall_seconds"], 2.5)
        report_path = self.root / "report.json"
        report_path.write_text(json.dumps(report))
        vs_image = self.root / "sonic-vs.img.gz"
        vs_image.write_bytes(gzip.compress(b"VS fixture"))
        integrated = verify.verify_integration(
            report_path, self.root / "read/docker-sysmgr.gz", vs_image
        )
        self.assertTrue(integrated["vs_integration"]["sysmgr_archive_matches_read_pass"])

    def test_rejects_local_disk_failed_or_different_read_actions(self):
        for changes in (
            {"runner": "processwrapper-sandbox", "cacheHit": False},
            {"runner": "disk cache hit", "cacheHit": True},
            {"runner": "remote cache hit", "cacheHit": False},
            {"runner": "remote cache hit", "cacheHit": True, "exitCode": 1},
            {"runner": "remote cache hit", "cacheHit": True, "status": "TIMEOUT"},
            {"runner": "remote cache hit", "cacheHit": True, "digest": {"hash": "b" * 64}},
            {"runner": "remote cache hit", "cacheHit": True, "digest": {}},
        ):
            with self.subTest(changes=changes):
                write_records(self.root / "read/execution.json", [dict(COMPILE, **changes)])
                with self.assertRaises(ValueError):
                    self.validate()

    def test_genrule_or_dependency_only_hits_are_not_proof(self):
        for changes in (
            {"mnemonic": "Genrule"}, {"targetLabel": "@@protobuf+//:libprotobuf"},
            {"remoteCacheable": False},
        ):
            with self.subTest(changes=changes):
                write_records(
                    self.root / "read/execution.json",
                    [dict(COMPILE, runner="remote cache hit", cacheHit=True, **changes)],
                )
                with self.assertRaisesRegex(ValueError, "No remotely cacheable sysmgr"):
                    self.validate()

    def test_write_pass_cannot_use_a_cache(self):
        write_records(
            self.root / "write/execution.json",
            [dict(COMPILE, runner="remote cache hit", cacheHit=True)],
        )
        with self.assertRaisesRegex(ValueError, "Write pass used cached"):
            self.validate()

    def test_complete_manifest_not_just_version_is_checked(self):
        manifest = json.loads(self.normal.read_text())
        manifest["cli"]["config"] = "different.plugin"
        self.normal.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "complete Make-generated manifest"):
            self.validate()

    def test_changed_warm_archive_and_wrong_platform_fail(self):
        manifest = json.loads(self.normal.read_text())
        write_archive(self.root / "read/docker-sysmgr.gz", "docker-sysmgr", manifest, "arm64")
        with self.assertRaisesRegex(ValueError, "Wrong image platform"):
            self.validate()

    def test_full_make_path_cannot_substitute_a_different_archive(self):
        report = self.root / "report.json"
        report.write_text(json.dumps(self.validate()))
        changed = self.root / "changed.gz"
        changed.write_bytes(b"different archive")
        with self.assertRaisesRegex(ValueError, "did not use the validated"):
            verify.verify_integration(report, changed, changed)

    def test_failed_or_missing_build_events_cannot_pass(self):
        path = self.root / "read/events.json"
        for events in (
            [{"finished": {"overallSuccess": False}}],
            [],
            [{"finished": {"overallSuccess": True}}],
            [{"finished": {"overallSuccess": True}},
             {"buildMetrics": {"timingMetrics": {"wallTimeInMs": "0"}}}],
        ):
            with self.subTest(events=events):
                path.write_text("".join(json.dumps(event) + "\n" for event in events))
                with self.assertRaises(ValueError):
                    self.validate()

    def test_records_are_decoded_once_and_truncation_fails(self):
        path = self.root / "large.json"
        record = dict(COMPILE, inputs=[{"path": f"file-{index}"} for index in range(5000)])
        write_records(path, [record, COMPILE])
        with patch.object(verify.json, "loads", wraps=json.loads) as loads:
            self.assertEqual(list(verify.records(path)), [record, COMPILE])
            self.assertEqual(loads.call_count, 2)
        path.write_text('{\n  "mnemonic": "CppCompile"\n')
        with self.assertRaisesRegex(ValueError, "Truncated"):
            list(verify.records(path))

    def test_bazel_adjacent_objects_and_compact_streams(self):
        path = self.root / "adjacent.json"
        records = [COMPILE, dict(COMPILE, inputs=[{"path": "literal }{ braces"}])]
        for indent in (None, 2):
            with self.subTest(indent=indent):
                path.write_text("".join(json.dumps(record, indent=indent) for record in records))
                self.assertEqual(list(verify.records(path)), records)


class CacheMountTest(unittest.TestCase):
    def test_remote_configuration_does_not_leak_into_nested_workspaces(self):
        remote_import = "try-import /etc/sonic-bazel-remote.bazelrc"
        self.assertIn(remote_import, (ROOT / ".bazelrc").read_text().splitlines())
        self.assertNotIn(
            remote_import, (ROOT / "tools/bazel/slave.bazelrc").read_text().splitlines()
        )

    def test_optional_mount_and_fail_closed_configuration(self):
        source = (ROOT / "Makefile.work").read_text()
        fragment = source.split("# Create a volume to store Bazel caches into\n", 1)[1]
        fragment = fragment.split("# User name and tag", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            cache.mkdir()
            rc = root / "remote.bazelrc"
            rc.write_text("common --remote_cache=grpcs://cache.example:9092\n")
            fixture = root / "Makefile"
            fixture.write_text(fragment + "\nall:\n\t@echo '$(DOCKER_RUN)'\n")

            def run(cache_path, rc_path):
                return subprocess.run(
                    [
                        "make", "-s", "-f", str(fixture),
                        f"SONIC_BAZEL_USER_CACHE_SOURCE={cache_path}",
                        f"SONIC_BAZEL_REMOTE_CACHE_CONFIG={rc_path}",
                    ],
                    capture_output=True, text=True,
                )

            local = run(cache, "")
            self.assertEqual(local.returncode, 0, local.stderr)
            self.assertIn("/etc/bazel.bazelrc:ro", local.stdout)
            self.assertNotIn("/etc/sonic-bazel-remote.bazelrc", local.stdout)
            remote = run(cache, rc)
            self.assertEqual(remote.returncode, 0, remote.stderr)
            self.assertIn(f"{rc}:/etc/sonic-bazel-remote.bazelrc:ro", remote.stdout)
            for invalid in (root / "missing", cache):
                failed = run(cache, invalid)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn("readable regular file", failed.stderr)
            failed = run(root / "missing-cache", rc)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("writable SONIC_BAZEL_USER_CACHE_SOURCE", failed.stderr)
            fallback = run(root / "missing-cache", "")
            self.assertEqual(fallback.returncode, 0)
            self.assertIn("building with caches local", fallback.stderr)


class ManifestMakeTest(unittest.TestCase):
    def test_normal_debug_partial_and_legacy_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            templates = root / "files/build_templates"
            templates.mkdir(parents=True)
            shutil.copyfile(
                ROOT / "files/build_templates/manifest.json.j2", templates / "manifest.json.j2"
            )
            shutil.copytree(ROOT / "scripts/j2cli", root / "scripts/j2cli")
            (templates / "sysmgr.service.j2").touch()
            docker = root / "docker"
            docker.mkdir()
            (docker / "manifest.part.json.j2").write_text(
                '{"service":{"requires":["database"]},"custom":{"build":"{{ version_suffix }}"}}'
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            renderer = bin_dir / "j2"
            renderer.write_text(
                "#!/usr/bin/env python3\n"
                "import importlib.util, os, sys\n"
                "from pathlib import Path\n"
                "from jinja2 import Environment\n"
                "spec = importlib.util.spec_from_file_location('filters', sys.argv[2])\n"
                "filters = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(filters)\n"
                "env = Environment()\n"
                "env.filters.update(filters.extra_filters())\n"
                "print(env.from_string(Path(sys.argv[3]).read_text()).render(**os.environ))\n"
            )
            renderer.chmod(0o755)
            bazel = bin_dir / "bazel"
            bazel.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "from pathlib import Path\n"
                "name = sys.argv[-1].split(':')[-1]\n"
                "normal = json.loads(Path('target/docker-sysmgr.manifest.json').read_text())\n"
                "assert normal['package']['version'] == '2.3.4+vendor'\n"
                "if '-dbg' in name:\n"
                "    debug = json.loads(Path('target/docker-sysmgr-dbg.manifest.json').read_text())\n"
                "    assert debug['package']['version'] == '2.3.4+vendor.dbg'\n"
                "out = Path('out') / name\n"
                "if sys.argv[1] == 'build':\n"
                "    out.parent.mkdir(exist_ok=True)\n"
                "    out.write_text(name)\n"
                "else:\n"
                "    print(out)\n"
            )
            bazel.chmod(0o755)
            source = (ROOT / "slave.mk").read_text()
            rules = source.split("# Keep normal and debug metadata separate:", 1)[1]
            rules = "# Keep normal and debug metadata separate:" + rules.split(
                "# Targets for building docker images", 1
            )[0]
            archive_rules = source.split("# Targets for building docker images (and debug images) with Bazel.", 1)[1]
            archive_rules = archive_rules.split("SONIC_TARGET_LIST +=", 1)[0]
            fixture = root / "Makefile"
            fixture.write_text(
                "SHELL := /bin/bash\n.SHELLFLAGS := -e -c\n.ONESHELL:\n.SECONDEXPANSION:\n"
                "TARGET_PATH := target\nDBG_IMAGE_MARK := dbg\n"
                "SONIC_BAZEL_DOCKER_IMAGES := docker-sysmgr.gz\n"
                "SONIC_BAZEL_DBG_DOCKER_IMAGES := docker-sysmgr-dbg.gz\n"
                f"include {ROOT / 'rules/functions'}\n"
                "HEADER :=\nFOOTER :=\nLOG :=\nPROJECT_ROOT := .\n"
                "docker-sysmgr.gz_PATH := docker\n"
                "docker-sysmgr.gz_VERSION := 2.3.4+vendor\n"
                "docker-sysmgr.gz_CONTAINER_NAME := sysmgr\n"
                "docker-sysmgr.gz_PACKAGE_NAME := sysmgr\n"
                "docker-sysmgr.gz_SERVICE_AFTER := database\n"
                "docker-sysmgr.gz_WARM_SHUTDOWN_AFTER := database\n"
                "docker-sysmgr.gz_CONTAINER_VOLUMES := /etc/sonic:/etc/sonic:ro\n"
                "docker-sysmgr.gz_CLI_CONFIG_PLUGIN := custom.plugin\n"
                ".PHONY: FORCE_BAZEL\nFORCE_BAZEL:\n" + rules + archive_rules +
                "\nlegacy:\n\t$(call generate_manifest,docker-sysmgr)\n"
            )
            (root / ".platform").write_text("vs\n")
            env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
            targets = ["target/docker-sysmgr.manifest.json", "target/docker-sysmgr-dbg.manifest.json"]
            result = subprocess.run(
                ["make", "-s", "-j2", "-f", str(fixture), *targets],
                cwd=root, env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            normal, debug = [json.loads((root / target).read_text()) for target in targets]
            self.assertEqual(normal["package"]["version"], "2.3.4+vendor")
            self.assertEqual(debug["package"]["version"], "2.3.4+vendor.dbg")
            self.assertTrue(normal["service"]["host-service"])
            self.assertEqual(normal["service"]["after"], ["database"])
            self.assertEqual(debug["service"]["warm-shutdown"]["after"], ["database"])
            self.assertEqual(normal["service"]["requires"], ["database"])
            self.assertEqual(normal["container"]["volumes"], ["/etc/sonic:/etc/sonic:ro"])
            self.assertEqual(normal["cli"]["config"], "custom.plugin")
            self.assertEqual(debug["custom"]["build"], "dbg")
            result = subprocess.run(
                ["make", "-s", "-j2", "-f", str(fixture),
                 "target/docker-sysmgr.gz", "target/docker-sysmgr-dbg.gz"],
                cwd=root, env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for stem in ("docker-sysmgr", "docker-sysmgr-dbg"):
                self.assertEqual((root / "target" / (stem + ".gz")).read_text(), stem + ".gz")
            result = subprocess.run(
                ["make", "-s", "-f", str(fixture), "legacy"],
                cwd=root, env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((docker / "manifest.json").read_text()), normal)
            (docker / "manifest.part.json.j2").unlink()
            result = subprocess.run(
                ["make", "-s", "-j2", "-f", str(fixture),
                 "docker-sysmgr.gz_VERSION=4.5.6", *targets],
                cwd=root, env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            normal, debug = [json.loads((root / target).read_text()) for target in targets]
            self.assertEqual(normal["package"]["version"], "4.5.6")
            self.assertEqual(debug["package"]["version"], "4.5.6+dbg")
            self.assertNotIn("custom", debug)


class PipelineTest(unittest.TestCase):
    def test_uncached_vs_budget_and_canceled_evidence(self):
        pipeline = yaml.safe_load(
            (ROOT / ".azure-pipelines/azure-pipelines-bazel-cache.yml").read_text()
        )
        job = pipeline["jobs"][0]
        self.assertEqual(job["timeoutInMinutes"], 600)
        self.assertEqual(job["cancelTimeoutInMinutes"], 10)
        self.assertEqual(
            job["steps"][-1]["condition"],
            "and(always(), eq(variables['PR28005_RESULTS_READY'], 'true'))",
        )

    def test_manual_checkout_and_existing_cache_credentials(self):
        pipeline = yaml.safe_load(
            (ROOT / ".azure-pipelines/azure-pipelines-bazel-cache.yml").read_text()
        )
        self.assertEqual(pipeline["pr"], "none")
        self.assertEqual(pipeline["trigger"], "none")
        self.assertNotIn("resources", pipeline)
        self.assertEqual(pipeline["variables"][0]["group"], "SONiC-Bazel-Cache")
        steps = pipeline["jobs"][0]["steps"]
        self.assertEqual(steps[0]["checkout"], "self")
        self.assertEqual(steps[0]["submodules"], "recursive")
        self.assertEqual(
            steps[1]["inputs"]["secureFile"], "sonic-bazel-cache-ci-certs.tar.gz"
        )
        self.assertIn("PR28005_RESULTS_READY", steps[-1]["condition"])
        host = (ROOT / "tools/bazel/ci/pr28005_cache_ci.sh").read_text()
        self.assertIn("BAZEL_MIN_READINESS=experimental", host)
        self.assertIn("SONIC_DPKG_CACHE_METHOD=none", host)
        self.assertIn("SONIC_BUILD_VARS=--old-file=target/docker-config-engine-trixie.gz", host)
        self.assertNotIn("songqing_native", host)

    def test_private_cache_cleanup_handles_readonly_directories(self):
        host = (ROOT / "tools/bazel/ci/pr28005_cache_ci.sh").read_text()
        cleanup = host.split(
            'run_root="$(mktemp -d "$AGENT_TEMPDIRECTORY/pr28005-cache.XXXXXXXX")"\n', 1
        )[1].split("umask 077\n", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("outside the private cache")
            outside.chmod(0o555)
            try:
                for status in (0, 7):
                    with self.subTest(status=status):
                        run_root = root / f"run-{status}"
                        sdk = run_root / "cache/python-sdk/lib"
                        sdk.mkdir(parents=True)
                        module = sdk / "module.py"
                        module.write_text("pass\n")
                        module.chmod(0o444)
                        sdk.chmod(0o555)
                        (run_root / "external-link").symlink_to(
                            outside, target_is_directory=True
                        )
                        result = subprocess.run(
                            [
                                "bash", "-c",
                                'set -euo pipefail\nrun_root="$1"\n' + cleanup + '\nexit "$2"',
                                "cleanup-test", str(run_root), str(status),
                            ],
                            capture_output=True, text=True,
                        )
                        self.assertEqual(result.returncode, status, result.stderr)
                        self.assertFalse(run_root.exists())
                        self.assertEqual(outside.stat().st_mode & 0o777, 0o555)
                        self.assertEqual(
                            (outside / "keep.txt").read_text(), "outside the private cache"
                        )
            finally:
                outside.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
