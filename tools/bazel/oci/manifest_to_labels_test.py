import json
from pathlib import Path
import tempfile
import unittest

from manifest_to_labels import convert


class ManifestLabelsTest(unittest.TestCase):
    def test_complete_manifest_is_one_label_line(self):
        manifest = {
            "package": {"name": "sysmgr", "version": "2.3.4+vendor.dbg"},
            "service": {"after": ["database"], "host-service": True},
            "container": {"volumes": ["/etc/sonic:/etc/sonic:ro"]},
            "cli": {"config": "plugin\nwith=characters"},
            "custom": {"nested": [True, False, None]},
        }
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "manifest.json"
            out = Path(directory) / "image.labels"
            src.write_text(json.dumps(manifest))
            convert(src, out)
            lines = out.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        key, value = lines[0].split("=", 1)
        self.assertEqual(key, "com.azure.sonic.manifest")
        self.assertEqual(json.loads(value), manifest)

    def test_invalid_manifest_does_not_create_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "manifest.json"
            out = Path(directory) / "image.labels"
            for invalid in ("[]", "null", '"text"', "{broken"):
                with self.subTest(invalid=invalid):
                    src.write_text(invalid)
                    with self.assertRaises(ValueError):
                        convert(src, out)
                    self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
