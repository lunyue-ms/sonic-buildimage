"""Validate sysmgr cache proofs, nightly Bazel selection and VS provenance."""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile


def require(condition, message):
    if not condition:
        raise ValueError(message)


def records(path):
    # Bazel joins pretty-printed objects with "}{", without a newline.
    # Decode completed records once, not repeatedly while reading large inputs.
    decoder = json.JSONDecoder()
    parts = []
    with Path(path).open() as stream:
        for line in stream:
            if parts and line.startswith("}"):
                parts.append("}")
                record = json.loads("".join(parts))
                require(isinstance(record, dict), "Expected a Bazel JSON object")
                yield record
                parts = []
                line = line[1:]
            if not line.strip():
                continue
            if not parts and line.startswith("{") and line.rstrip().endswith("}"):
                offset = 0
                while offset < len(line):
                    if line[offset].isspace():
                        offset += 1
                        continue
                    record, offset = decoder.raw_decode(line, offset)
                    require(isinstance(record, dict), "Expected a Bazel JSON object")
                    yield record
            else:
                parts.append(line)
    require(not parts, "Truncated Bazel execution log")


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compile_actions(path, reading):
    runners = Counter()
    actions = {}
    for record in records(path):
        runner = record.get("runner", "")
        runners[runner] += 1
        require(not record.get("status") and not record.get("exitCode"), "Failed Bazel spawn")
        require(runner != "disk cache hit", "Disk cache is not remote-cache proof")
        if not reading:
            require(not record.get("cacheHit", False), "Write pass used cached results")
        if record.get("mnemonic") != "CppCompile" or not record.get("remoteCacheable"):
            continue
        target = record.get("targetLabel", "")
        if not re.match(r"@{1,2}sonic[-_]sysmgr(?:[+~][^/]*)?//", target):
            continue
        digest = record.get("digest", {}).get("hash", "")
        require(re.fullmatch(r"[0-9a-f]{64}", digest), "Missing sysmgr action digest")
        if reading:
            require(
                runner == "remote cache hit" and record.get("cacheHit") is True,
                "Sysmgr compile fell back to local execution",
            )
        actions[digest] = target
    require(actions, "No remotely cacheable sysmgr CppCompile actions")
    return actions, dict(sorted(runners.items()))


def inspect_archive(path, stem, expected):
    with tarfile.open(path, "r:gz") as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
        require(len(manifest) == 1, "Expected one docker image")
        require(manifest[0]["RepoTags"] == [stem + ":latest"], "Unexpected docker image tag")
        config_bytes = archive.extractfile(manifest[0]["Config"]).read()
    config = json.loads(config_bytes)
    require(config["os"] == "linux" and config["architecture"] == "amd64", "Wrong image platform")
    label = config.get("config", {}).get("Labels", {}).get("com.azure.sonic.manifest")
    require(isinstance(label, str), "Missing SONiC manifest label")
    actual = json.loads(label)
    require(actual == expected, "OCI label differs from the complete Make-generated manifest")
    require(actual["package"]["name"] == "sysmgr", "Wrong package name")
    require(actual["service"]["name"] == "sysmgr", "Wrong service name")
    require(actual["package"]["version"], "Empty package version")
    return {
        "archive_sha256": file_hash(path),
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "sonic_manifest": actual,
    }


def timing(path):
    metrics = []
    finished = []
    with Path(path).open() as stream:
        for line in stream:
            event = json.loads(line)
            if "buildMetrics" in event:
                metrics.append(event["buildMetrics"]["timingMetrics"])
            if "finished" in event:
                finished.append(event["finished"])
    require(len(finished) == 1 and finished[0].get("overallSuccess") is True,
            "Bazel build did not finish successfully")
    require(len(metrics) == 1, "Missing unique Bazel timing metrics")
    require(int(metrics[0]["wallTimeInMs"]) > 0, "Invalid Bazel wall time")
    return {
        "wall_seconds": int(metrics[0]["wallTimeInMs"]) / 1000,
        "analysis_seconds": int(metrics[0].get("analysisPhaseTimeInMs", 0)) / 1000,
        "execution_seconds": int(metrics[0].get("executionPhaseTimeInMs", 0)) / 1000,
    }


def verify_cache(root, normal_manifest, debug_manifest):
    written, write_runners = compile_actions(root / "write/execution.json", False)
    hits, read_runners = compile_actions(root / "read/execution.json", True)
    require(written.keys() == hits.keys(), "Cold and warm sysmgr action digests differ")
    images = {}
    for stem, manifest_path in (
        ("docker-sysmgr", normal_manifest),
        ("docker-sysmgr-dbg", debug_manifest),
    ):
        expected = json.loads(manifest_path.read_text())
        cold = inspect_archive(root / "write" / (stem + ".gz"), stem, expected)
        warm = inspect_archive(root / "read" / (stem + ".gz"), stem, expected)
        require(cold == warm, "Cold and warm archive outputs differ")
        images[stem] = warm
    return {
        "sysmgr_cpp_actions": len(written),
        "sysmgr_cpp_remote_hits": len(hits),
        "matching_action_samples": [
            {"digest": digest, "target": hits[digest]} for digest in sorted(hits)[:20]
        ],
        "write_runners": write_runners,
        "read_runners": read_runners,
        "write_timing": timing(root / "write/events.json"),
        "read_timing": timing(root / "read/events.json"),
        "images": images,
    }


def verify_integration(report_path, sysmgr_archive, vs_image):
    report = json.loads(report_path.read_text())
    require(
        file_hash(sysmgr_archive) == report["images"]["docker-sysmgr"]["archive_sha256"],
        "VS integration did not use the validated sysmgr archive",
    )
    require(vs_image.stat().st_size > 0, "Empty VS image")
    report["vs_integration"] = {
        "sysmgr_archive_matches_read_pass": True,
        "image_bytes": vs_image.stat().st_size,
        "image_sha256": file_hash(vs_image),
    }
    return report


def verify_nightly(target):
    manifest = json.loads((target / "docker-sysmgr.manifest.json").read_text())
    image = inspect_archive(target / "docker-sysmgr.gz", "docker-sysmgr", manifest)
    log = (target / "docker-sysmgr.gz.log").read_text()
    require("INFO: Build completed successfully" in log,
            "Missing a successful sysmgr Bazel build in the Make log")
    summaries = re.findall(r"INFO:\s+(\d+) process(?:es)?[:.](.*)", log)
    require(summaries, "Missing sysmgr Bazel process statistics")
    count, runners = summaries[-1]
    remote_hits = re.findall(r"(\d+) remote cache hit", runners)
    warnings = len(re.findall(r"WARNING:.*(?:remote cache|cache.*remote)", log, re.IGNORECASE))
    vs_image = target / "sonic-vs.img.gz"
    require(vs_image.stat().st_size > 0, "Empty VS image")
    return {
        "images": {"docker-sysmgr": image},
        "last_sysmgr_build": {
            "processes": int(count),
            "remote_cache_hits": sum(map(int, remote_hits)),
            "remote_cache_warnings": warnings,
        },
        "vs_image": {
            "bytes": vs_image.stat().st_size,
            "sha256": file_hash(vs_image),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    cache = commands.add_parser("cache")
    for name in ("root", "normal_manifest", "debug_manifest", "report"):
        cache.add_argument(name, type=Path)
    integration = commands.add_parser("integration")
    for name in ("report", "sysmgr_archive", "vs_image"):
        integration.add_argument(name, type=Path)
    nightly = commands.add_parser("nightly")
    nightly.add_argument("target", type=Path)
    nightly.add_argument("report", type=Path)
    args = parser.parse_args()
    if args.mode == "cache":
        result = verify_cache(args.root, args.normal_manifest, args.debug_manifest)
    elif args.mode == "integration":
        result = verify_integration(args.report, args.sysmgr_archive, args.vs_image)
    else:
        result = verify_nightly(args.target)
        for name in ("BUILD_BUILDID", "SYSTEM_DEFINITIONID"):
            require(os.environ.get(name), f"Missing CI provenance variable: {name}")
        result["source_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
        result["bazel_version"] = Path(".bazelversion").read_text().strip()
        result["build_id"] = os.environ["BUILD_BUILDID"]
        result["pipeline_id"] = os.environ["SYSTEM_DEFINITIONID"]
        if result["last_sysmgr_build"]["remote_cache_warnings"]:
            print("##vso[task.logissue type=warning]Sysmgr reported remote-cache warnings; "
                  "inspect its archived Make log")
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(f"PASS: remote-cache {args.mode} validation")


if __name__ == "__main__":
    main()
