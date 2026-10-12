#!/usr/bin/env python3
import collections
import hashlib
import json
import pathlib
import re
import sys
import tarfile
import zipfile


def require(condition, message):
    if not condition:
        raise ValueError(message)


def records(path):
    decoder = json.JSONDecoder()
    pending = ""
    eof = False
    with pathlib.Path(path).open() as stream:
        while True:
            pending = pending.lstrip()
            if pending:
                try:
                    record, end = decoder.raw_decode(pending)
                except json.JSONDecodeError:
                    if eof:
                        raise
                else:
                    require(isinstance(record, dict), "Expected a JSON object")
                    yield record
                    pending = pending[end:]
                    continue
            if eof:
                return
            chunk = stream.read(65536)
            pending += chunk
            eof = not chunk


def statistics(path):
    runners = collections.Counter()
    for record in records(path):
        runners[record["runner"]] += 1
    require(runners, "Empty Bazel execution log")
    return dict(sorted(runners.items()))


def verify_cache(write_log, read_log, write_image, read_image):
    require(
        json.loads(pathlib.Path(write_image).read_text())
        == json.loads(pathlib.Path(read_image).read_text()),
        "Downloaded OCI image differs from the locally built image",
    )
    written = set()
    for record in records(write_log):
        require(not record.get("cacheHit", False), "Write pass used a cache")
        if record["mnemonic"] == "CppCompile" and record.get("remoteCacheable", False):
            require(not record.get("status") and not record.get("exitCode"), "Write compile failed")
            written.add(record["digest"]["hash"])
    require(written, "Write pass contains no remotely cacheable C++ compilation")
    hits = []
    for record in records(read_log):
        require(record["runner"] != "disk cache hit", "Read pass used disk cache")
        if record["mnemonic"] == "CppCompile" and record.get("remoteCacheable", False):
            require(not record.get("status") and not record.get("exitCode"), "Read compile failed")
            if record["runner"] == "remote cache hit" and record.get("cacheHit") is True:
                require(record["digest"]["hash"] in written, "Read hit has a different action key")
                hits.append({
                    "action_digest": record["digest"]["hash"],
                    "target": record["targetLabel"],
                    "outputs": record["listedOutputs"],
                    "runner": record["runner"],
                    "cacheHit": True,
                })
    require(hits, "No matching C++ compile remotely hit; local fallback is not proof")
    return {
        "write_cpp_actions": len(written),
        "read_cpp_remote_hits": len(hits),
        "read_runners": statistics(read_log),
        "matching_hits": hits,
        "oci_image_matches": True,
    }


def verify_warm_build(path):
    finished = []
    metrics = []
    for event in records(path):
        if "finished" in event:
            finished.append(event["finished"])
        if "buildMetrics" in event:
            metrics.append(event["buildMetrics"])
    require(len(finished) == 1 and finished[0].get("overallSuccess") is True,
            "Expected one successful Bazel build-finished event")
    require(len(metrics) == 1, "Expected one Bazel build-metrics event")
    summary = metrics[0]["actionSummary"]
    runners = collections.Counter()
    for runner in summary["runnerCount"]:
        if runner["name"] == "total":
            continue
        count = int(runner["count"])
        require(count >= 0, "Invalid runner count")
        runners[runner["name"]] += count
    require(not runners["disk cache hit"], "Warm build used disk cache")
    require(runners["remote cache hit"] > 0,
            "No cross-run remote cache hits; local fallback is not proof")
    timing = metrics[0]["timingMetrics"]
    wall_ms = int(timing["wallTimeInMs"])
    require(wall_ms > 0, "Missing positive Bazel wall time")
    return {
        "bazel_wall_seconds": wall_ms / 1000,
        "analysis_seconds": int(timing.get("analysisPhaseTimeInMs", 0)) / 1000,
        "execution_seconds": int(timing.get("executionPhaseTimeInMs", 0)) / 1000,
        "actions_executed": int(summary["actionsExecuted"]),
        "remote_cache_hits": runners["remote cache hit"],
        "runners": dict(sorted((name, count) for name, count in runners.items() if count)),
    }


def file_hash(path, algorithm="sha256"):
    digest = hashlib.new(algorithm)
    with pathlib.Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_oci(layout):
    layout = pathlib.Path(layout)
    require(json.loads((layout / "oci-layout").read_text())["imageLayoutVersion"] == "1.0.0",
            "Invalid OCI layout version")
    seen = set()
    configurations = []

    def check(descriptor):
        digest = descriptor["digest"]
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", digest), "Invalid OCI digest")
        path = layout / "blobs/sha256" / digest.split(":")[1]
        require(path.stat().st_size == descriptor["size"], "OCI descriptor size mismatch")
        if digest in seen:
            return
        require(file_hash(path) == digest.split(":")[1], "OCI blob checksum mismatch")
        seen.add(digest)
        if descriptor["mediaType"].endswith("+json"):
            data = json.loads(path.read_text())
            for child in data.get("manifests", []) + data.get("layers", []):
                check(child)
            if "digest" in data.get("config", {}):
                check(data["config"])
            if "architecture" in data:
                require(data["architecture"] == "amd64" and data["os"] == "linux",
                        "Expected a linux/amd64 OCI image")
                configurations.append(data)

    manifests = json.loads((layout / "index.json").read_text())["manifests"]
    require(len(manifests) == 1, "Expected one sysmgr OCI manifest")
    check(manifests[0])
    require(len(configurations) == 1, "Expected one sysmgr image configuration")
    labels = configurations[0]["config"]["Labels"]
    manifest = json.loads(labels["com.azure.sonic.manifest"])
    require(manifest["package"]["name"] == "sysmgr" and manifest["service"]["name"] == "sysmgr",
            "Invalid sysmgr package or service name")
    require(manifest["package"]["version"] == "1.0.0", "Invalid sysmgr package version")
    return {"manifest_digest": manifests[0]["digest"], "blob_count": len(seen),
            "architecture": "amd64", "os": "linux", "sonic_manifest": manifest}


def verify_installer(image_path, zip_path):
    image_path = pathlib.Path(image_path)
    zip_path = pathlib.Path(zip_path)
    with image_path.open("rb") as image:
        header = bytearray()
        while True:
            line = image.readline(16384)
            require(line and len(header) < 65536, "Missing installer exit_marker")
            header.extend(line)
            if line.rstrip(b"\n") == b"exit_marker":
                break
        offset = image.tell()
        size_match = re.search(rb"^payload_image_size=(\d+)$", header, re.M)
        sha_match = re.search(rb"^payload_sha1=([0-9a-f]{40})$", header, re.M)
        require(size_match and sha_match, "Missing installer payload metadata")
        remaining = int(size_match[1])
        payload_digest = hashlib.sha1()
        image_digest = hashlib.sha256(header)
        while remaining:
            chunk = image.read(min(8 * 1024 * 1024, remaining))
            require(chunk, "Truncated installer payload")
            payload_digest.update(chunk)
            image_digest.update(chunk)
            remaining -= len(chunk)
        trailing = image.read(2)
        require(trailing in (b"", b"\n"), "Unexpected installer trailing bytes")
        image_digest.update(trailing)
        require(payload_digest.hexdigest() == sha_match[1].decode(), "Installer payload SHA1 mismatch")
        image.seek(offset)
        with tarfile.open(fileobj=image, mode="r:") as archive:
            members = {
                member.name[2:] if member.name.startswith("./") else member.name: member
                for member in archive
            }
            for name in ("installer/install.sh", "installer/fs.zip",
                         "installer/platforms_asic", "installer/platform.conf"):
                require(name in members, "Missing installer member: " + name)
            require(members["installer/fs.zip"].size == zip_path.stat().st_size,
                    "Embedded ZIP size mismatch")
            embedded_digest = hashlib.sha256()
            with archive.extractfile(members["installer/fs.zip"]) as stream:
                for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    embedded_digest.update(chunk)
            require(embedded_digest.hexdigest() == file_hash(zip_path), "Embedded ZIP checksum mismatch")
    kernel_release = "6.12.41+deb13-sonic-amd64"
    with zipfile.ZipFile(zip_path) as payload:
        for member in payload.infolist():
            require(not member.flag_bits & 9, "ONIE-incompatible ZIP flags")
        for name in ("fs.squashfs", "platform.tar.gz", "dockerfs.tar.gz",
                     "boot/vmlinuz-" + kernel_release, "boot/initrd.img-" + kernel_release):
            require(name in payload.namelist(), "Missing payload member: " + name)
        require(payload.testzip() is None, "Payload ZIP CRC mismatch")
    return {"image_bytes": image_path.stat().st_size, "image_sha256": image_digest.hexdigest(),
            "payload_sha1": payload_digest.hexdigest(), "kernel_release": kernel_release,
            "embedded_zip_matches": True, "zip_crc_valid": True}


def main():
    mode, *args = sys.argv[1:]
    if mode == "cache" and len(args) == 5:
        result = verify_cache(*args[:-1])
    elif mode == "warm" and len(args) == 2:
        result = verify_warm_build(args[0])
    elif mode == "oci" and len(args) == 2:
        result = verify_oci(args[0])
    elif mode == "installer" and len(args) == 3:
        result = verify_installer(*args[:-1])
    elif mode == "stats" and len(args) == 2:
        result = statistics(args[0])
    else:
        raise ValueError("Expected cache, warm, oci, installer or stats arguments")
    text = json.dumps(result, indent=2) + "\n"
    pathlib.Path(args[-1]).write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
