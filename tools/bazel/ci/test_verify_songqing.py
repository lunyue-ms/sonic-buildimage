import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import tarfile
import tempfile
import unittest
import zipfile

import verify_songqing as verify


class NativeVerificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)

    def json_file(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        return path

    def execution_log(self, name, records):
        path = self.root / name
        path.write_text("\n".join(json.dumps(record, indent=2) for record in records))
        return path

    def cache_inputs(self):
        record = {
            "mnemonic": "CppCompile", "remoteCacheable": True,
            "runner": "linux-sandbox", "cacheHit": False,
            "digest": {"hash": "a" * 64},
            "targetLabel": "@@sonic-sysmgr+//:rebootbackend",
            "listedOutputs": ["bazel-out/k8-opt/bin/sysmgr.o"],
        }
        read = dict(record, runner="remote cache hit", cacheHit=True)
        return record, read, self.json_file("write-image.json", {"manifest_digest": "sha256:x"}), \
            self.json_file("read-image.json", {"manifest_digest": "sha256:x"})

    def test_real_compile_hit_and_output_match(self):
        write, read, write_image, read_image = self.cache_inputs()
        result = verify.verify_cache(self.execution_log("write.json", [write]),
                                     self.execution_log("read.json", [read]), write_image, read_image)
        self.assertEqual(result["read_cpp_remote_hits"], 1)
        self.assertTrue(result["oci_image_matches"])

    def test_local_fallback_and_disk_hit_are_not_remote_proof(self):
        for runner in ("linux-sandbox", "disk cache hit"):
            with self.subTest(runner=runner):
                write, read, write_image, read_image = self.cache_inputs()
                read["runner"] = runner
                read["cacheHit"] = runner == "disk cache hit"
                with self.assertRaises(ValueError):
                    verify.verify_cache(self.execution_log("write.json", [write]),
                                        self.execution_log("read.json", [read]), write_image, read_image)

    def test_unrelated_hit_and_different_action_key_are_rejected(self):
        for field, value in (("mnemonic", "Genrule"), ("digest", {"hash": "b" * 64})):
            with self.subTest(field=field):
                write, read, write_image, read_image = self.cache_inputs()
                read[field] = value
                with self.assertRaises(ValueError):
                    verify.verify_cache(self.execution_log("write.json", [write]),
                                        self.execution_log("read.json", [read]), write_image, read_image)

    def test_cached_write_and_different_output_are_rejected(self):
        write, read, write_image, read_image = self.cache_inputs()
        write["cacheHit"] = True
        with self.assertRaises(ValueError):
            verify.verify_cache(self.execution_log("write.json", [write]),
                                self.execution_log("read.json", [read]), write_image, read_image)
        write["cacheHit"] = False
        read_image.write_text('{"manifest_digest": "sha256:y"}')
        with self.assertRaises(ValueError):
            verify.verify_cache(self.execution_log("write.json", [write]),
                                self.execution_log("read.json", [read]), write_image, read_image)

    def test_streaming_logs_cross_chunk_boundaries(self):
        path = self.execution_log("large.json", [{"padding": "x" * 70000}, {"runner": "linux-sandbox"}])
        self.assertEqual(len(list(verify.records(path))), 2)
        path.write_text('{"runner":')
        with self.assertRaises(json.JSONDecodeError):
            list(verify.records(path))
        path.write_text("")
        with self.assertRaises(ValueError):
            verify.statistics(path)

    def warm_events(self):
        return [
            {"finished": {"overallSuccess": True}},
            {"buildMetrics": {
                "actionSummary": {
                    "actionsExecuted": "6",
                    "runnerCount": [
                        {"name": "total", "count": 6},
                        {"name": "remote cache hit", "count": 3},
                        {"name": "internal", "count": 2},
                        {"name": "processwrapper-sandbox", "count": 1},
                    ],
                },
                "timingMetrics": {
                    "wallTimeInMs": "10000",
                    "analysisPhaseTimeInMs": "6000",
                    "executionPhaseTimeInMs": "4000",
                },
            }},
        ]

    def test_warm_build_cache_counts_and_timings(self):
        result = verify.verify_warm_build(self.execution_log("warm.json", self.warm_events()))
        self.assertEqual(result["remote_cache_hits"], 3)
        self.assertEqual(result["actions_executed"], 6)
        self.assertEqual(result["bazel_wall_seconds"], 10)
        self.assertEqual(result["analysis_seconds"], 6)
        self.assertEqual(result["execution_seconds"], 4)
        self.assertEqual(result["runners"]["processwrapper-sandbox"], 1)
        self.assertNotIn("total", result["runners"])

    def test_warm_build_requires_success_and_complete_metrics(self):
        for events in ([], self.warm_events()[:1], self.warm_events()[1:],
                       [{"finished": {"overallSuccess": False}}, self.warm_events()[1]],
                       self.warm_events() + [self.warm_events()[1]]):
            with self.subTest(events=events), self.assertRaises(ValueError):
                verify.verify_warm_build(self.execution_log("warm.json", events))

    def test_warm_build_rejects_disk_cache_no_remote_hits_and_invalid_metrics(self):
        for runner in ({"name": "disk cache hit", "count": 3},
                       {"name": "processwrapper-sandbox", "count": 3},
                       {"name": "remote cache hit", "count": -1}):
            events = self.warm_events()
            events[1]["buildMetrics"]["actionSummary"]["runnerCount"] = [runner]
            with self.subTest(runner=runner), self.assertRaises(ValueError):
                verify.verify_warm_build(self.execution_log("warm.json", events))
        events = self.warm_events()
        events[1]["buildMetrics"]["timingMetrics"]["wallTimeInMs"] = "0"
        with self.assertRaises(ValueError):
            verify.verify_warm_build(self.execution_log("warm.json", events))

    def make_oci(self):
        layout = self.root / "oci"
        (layout / "blobs/sha256").mkdir(parents=True)
        (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')

        def blob(data, media_type):
            encoded = data if isinstance(data, bytes) else json.dumps(data).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            (layout / "blobs/sha256" / digest).write_bytes(encoded)
            return {"digest": "sha256:" + digest, "size": len(encoded), "mediaType": media_type}

        metadata = {"package": {"name": "sysmgr", "version": "1.0.0"}, "service": {"name": "sysmgr"}}
        config = blob({"architecture": "amd64", "os": "linux", "config": {"Labels": {
            "com.azure.sonic.manifest": json.dumps(metadata),
        }}}, "application/vnd.oci.image.config.v1+json")
        layer = blob(b"layer", "application/vnd.oci.image.layer.v1.tar")
        manifest = blob({"config": config, "layers": [layer]}, "application/vnd.oci.image.manifest.v1+json")
        (layout / "index.json").write_text(json.dumps({"manifests": [manifest]}))
        return layout, layer

    def test_oci_layout_and_all_blob_checksums(self):
        layout, layer = self.make_oci()
        self.assertEqual(verify.verify_oci(layout)["blob_count"], 3)
        (layout / "blobs/sha256" / layer["digest"].split(":")[1]).write_bytes(b"wrong")
        with self.assertRaisesRegex(ValueError, "checksum"):
            verify.verify_oci(layout)

    def make_installer(self):
        payload = self.root / "fs.zip"
        names = ("fs.squashfs", "platform.tar.gz", "dockerfs.tar.gz",
                 "boot/vmlinuz-6.12.41+deb13-sonic-amd64", "boot/initrd.img-6.12.41+deb13-sonic-amd64")
        with zipfile.ZipFile(payload, "w") as archive:
            for name in names:
                archive.writestr(name, "dummy")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name in ("install.sh", "fs.zip", "platforms_asic", "platform.conf"):
                data = payload.read_bytes() if name == "fs.zip" else b"fixture"
                info = tarfile.TarInfo("installer/" + name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        data = buffer.getvalue()
        header = "payload_image_size={}\npayload_sha1={}\nexit_marker\n".format(
            len(data), hashlib.sha1(data).hexdigest())
        image = self.root / "sonic-vs.bin"
        image.write_bytes(header.encode() + data)
        return image, payload

    def test_installer_checksum_zip_and_kernel(self):
        image, payload = self.make_installer()
        report = verify.verify_installer(image, payload)
        self.assertEqual(report["image_sha256"], verify.file_hash(image))
        self.assertTrue(report["zip_crc_valid"])
        data = bytearray(image.read_bytes())
        data[-20] ^= 1
        image.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "SHA1"):
            verify.verify_installer(image, payload)

    def test_same_size_but_different_embedded_zip_is_rejected(self):
        image, payload = self.make_installer()
        original_size = payload.stat().st_size
        with zipfile.ZipFile(payload) as archive:
            names = archive.namelist()
        with zipfile.ZipFile(payload, "w") as archive:
            for name in names:
                archive.writestr(name, "bogus")
        self.assertEqual(payload.stat().st_size, original_size)
        with self.assertRaisesRegex(ValueError, "Embedded ZIP checksum"):
            verify.verify_installer(image, payload)

    def test_invalid_script_target_fails_before_network_or_credentials(self):
        script = pathlib.Path(__file__).with_name("songqing_native.sh")
        result = subprocess.run(["bash", str(script), "unexpected"], capture_output=True, text=True,
                                env={"PATH": os.environ["PATH"]})
        self.assertEqual(result.returncode, 1)
        self.assertIn("Unsupported native target", result.stderr)

    def test_private_sandbox_reserves_speakup_path_budget(self):
        script = pathlib.Path(__file__).with_name("songqing_native.sh")
        match = re.search(r'^sandbox_root="\$\(mktemp -d ([^\s)]+)\)"$',
                          script.read_text(), re.M)
        self.assertIsNotNone(match)
        sandbox = pathlib.Path(subprocess.check_output(
            ["/usr/bin/mktemp", "-d", match.group(1)], text=True,
        ).strip())
        self.addCleanup(sandbox.rmdir)
        self.assertEqual(sandbox.stat().st_mode & 0o777, 0o700)
        for counter in (1, 10565, 9999999):
            path = sandbox / (
                "bazel-sandbox." + "a" * 64
                + "/processwrapper-sandbox/{}/execroot/_main/external/"
                "sonic-linux-kernel++_repo_rules+kernel_sources/linux-6.12.41/"
                "drivers/accessibility/speakup/spk_priv_keyinfo.h"
            ).format(counter)
            with self.subTest(counter=counter):
                self.assertLess(len(os.fsencode(path)), 256)

    def test_controlled_patch_passes_p4c_bison_toolchain_environment(self):
        target = self.root / "src/p4lang/p4c-native-bazel.patch"
        target.parent.mkdir(parents=True)
        target.write_text(
            "@@ -22,4 +22,5 @@ def _genyacc_impl(ctx):\n"
            "-    ctx.actions.run_shell(\n"
            "+    ctx.actions.run(\n"
            "         use_default_shell_env = True,\n"
            "-        command = \"bison \" + \" \".join(args),\n"
            "+        executable = ctx.toolchains[\"@rules_bison//bison:toolchain_type\"]"
            ".bison_toolchain.bison_tool,\n"
            "+        arguments = args,\n"
            "         inputs = ctx.files.src,\n"
        )
        patch = pathlib.Path(__file__).with_name("songqing-inputs.patch")
        command = ["/usr/bin/git", "apply", "--include=src/p4lang/p4c-native-bazel.patch", str(patch)]
        subprocess.run(command, cwd=self.root, check=True, capture_output=True, text=True)
        result = target.read_text()
        self.assertIn("-        use_default_shell_env = True,\n", result)
        self.assertIn(".bison_toolchain.bison_env,\n", result)
        self.assertIn("+        arguments = args,\n", result)


if __name__ == "__main__":
    unittest.main()
