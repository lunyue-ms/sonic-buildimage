"""Serialize a SONiC manifest to rules_oci's newline-delimited label format."""

import argparse
import json
from pathlib import Path


def convert(src: Path, out: Path) -> None:
    manifest = json.loads(src.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("SONiC manifest must be a JSON object")
    value = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    out.write_text("com.azure.sonic.manifest=" + value + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    convert(args.src, args.out)


if __name__ == "__main__":
    main()
