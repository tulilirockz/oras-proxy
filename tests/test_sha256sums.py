from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import sha256sums  # noqa: E402


GOOD_DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64
FILENAME = "snapd-arm64_nightly.20261001.0.raw.zst"
OTHER_FILENAME = "usr-amd64_nightly.20261001.0.raw.zst"


def manifest_for(filename: str, digest: str = GOOD_DIGEST, size: int = 123) -> dict:
    return {
        "layers": [
            {
                "digest": f"sha256:{digest}",
                "size": size,
                "annotations": {"org.opencontainers.image.title": filename},
            }
        ]
    }


class ParseSumsTests(unittest.TestCase):
    def test_parses_text_and_binary_separators(self) -> None:
        entries, extra = sha256sums.parse_sums(
            f"{GOOD_DIGEST}  {FILENAME}\n{OTHER_DIGEST} *{OTHER_FILENAME}\n"
        )
        self.assertEqual(entries, {FILENAME: GOOD_DIGEST, OTHER_FILENAME: OTHER_DIGEST})
        self.assertEqual(extra, [])

    def test_keeps_unmanaged_lines(self) -> None:
        entries, extra = sha256sums.parse_sums(
            f"BEST-BEFORE-2027-01-01\n\n{GOOD_DIGEST}  {FILENAME}\n"
        )
        self.assertEqual(entries, {FILENAME: GOOD_DIGEST})
        self.assertEqual(extra, ["BEST-BEFORE-2027-01-01"])

    def test_ignores_blank_lines(self) -> None:
        entries, extra = sha256sums.parse_sums("\n \n")
        self.assertEqual(entries, {})
        self.assertEqual(extra, [])


class ManifestTests(unittest.TestCase):
    def test_entries_from_manifest_uses_layer_digest(self) -> None:
        entries = sha256sums.entries_from_manifest(
            "ghcr.io/org/image/snapd",
            "nightly.20261001.0-arm64",
            manifest_for(FILENAME, size=42),
            sha256sums.re.compile(sha256sums.DEFAULT_FILENAME_REGEX),
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, FILENAME)
        self.assertEqual(entries[0].sha256, GOOD_DIGEST)
        self.assertEqual(entries[0].digest, f"sha256:{GOOD_DIGEST}")
        self.assertEqual(entries[0].size, 42)

    def test_entries_from_manifest_skips_other_files(self) -> None:
        manifest = manifest_for("SHA256SUMS")
        entries = sha256sums.entries_from_manifest(
            "ghcr.io/org/image",
            "latest",
            manifest,
            sha256sums.re.compile(sha256sums.DEFAULT_FILENAME_REGEX),
        )
        self.assertEqual(entries, [])

    def test_entries_for_tag_resolves_index(self) -> None:
        manifests = {
            "ghcr.io/org/image/snapd:nightly.20261001.0-arm64": {
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {"digest": "sha256:111"},
                    {
                        "digest": "sha256:222",
                        "annotations": {
                            "vnd.docker.reference.type": "attestation-manifest"
                        },
                    },
                ],
            },
            "ghcr.io/org/image/snapd@sha256:111": manifest_for(FILENAME),
        }

        def fetch(ref: str) -> dict:
            return manifests[ref]

        entries = sha256sums.entries_for_tag(
            "ghcr.io/org/image/snapd",
            "nightly.20261001.0-arm64",
            fetch,
            sha256sums.re.compile(sha256sums.DEFAULT_FILENAME_REGEX),
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, FILENAME)

    def test_rejects_unsafe_entry_names(self) -> None:
        for name in ("/absolute.raw.zst", "../escape.raw.zst", "foo//bar.raw.zst", "100%.raw.zst"):
            with self.subTest(name=name):
                with self.assertRaises(sha256sums.ManifestError):
                    sha256sums.validate_entry_name(name)


class MergeTests(unittest.TestCase):
    def entry(self, name: str = FILENAME, digest: str = GOOD_DIGEST) -> sha256sums.Entry:
        return sha256sums.Entry(
            name=name,
            sha256=digest,
            repository="ghcr.io/org/image/snapd",
            tag="nightly.20261001.0-arm64",
            size=1,
        )

    def test_appends_new_entries(self) -> None:
        merged, added, updated, removed = sha256sums.merge_entries(
            {OTHER_FILENAME: OTHER_DIGEST},
            {FILENAME: self.entry()},
            overwrite=False,
            prune=False,
            existing_mapping={},
            scanned_repositories={"ghcr.io/org/image/snapd"},
        )
        self.assertEqual(
            merged, {OTHER_FILENAME: OTHER_DIGEST, FILENAME: GOOD_DIGEST}
        )
        self.assertEqual((added, updated, removed), (1, 0, 0))

    def test_same_entry_is_unchanged(self) -> None:
        merged, added, updated, removed = sha256sums.merge_entries(
            {FILENAME: GOOD_DIGEST},
            {FILENAME: self.entry()},
            overwrite=False,
            prune=False,
            existing_mapping={},
            scanned_repositories=set(),
        )
        self.assertEqual((added, updated, removed), (0, 0, 0))
        self.assertEqual(merged[FILENAME], GOOD_DIGEST)

    def test_conflicting_entry_fails_by_default(self) -> None:
        with self.assertRaises(SystemExit):
            sha256sums.merge_entries(
                {FILENAME: OTHER_DIGEST},
                {FILENAME: self.entry()},
                overwrite=False,
                prune=False,
                existing_mapping={},
                scanned_repositories=set(),
            )

    def test_conflicting_entry_can_be_overwritten(self) -> None:
        merged, added, updated, removed = sha256sums.merge_entries(
            {FILENAME: OTHER_DIGEST},
            {FILENAME: self.entry()},
            overwrite=True,
            prune=False,
            existing_mapping={},
            scanned_repositories=set(),
        )
        self.assertEqual(merged[FILENAME], GOOD_DIGEST)
        self.assertEqual((added, updated, removed), (0, 1, 0))

    def test_prune_only_removes_scanned_repositories(self) -> None:
        existing_mapping = {
            FILENAME: {"repository": "ghcr.io/org/image/snapd"},
            "other.raw.zst": {"repository": "ghcr.io/other/image"},
        }
        existing = {FILENAME: GOOD_DIGEST, "other.raw.zst": OTHER_DIGEST}
        merged, _, _, removed = sha256sums.merge_entries(
            existing,
            {},
            overwrite=False,
            prune=True,
            existing_mapping=existing_mapping,
            scanned_repositories={"ghcr.io/org/image/snapd"},
        )
        self.assertEqual(merged, {"other.raw.zst": OTHER_DIGEST})
        self.assertEqual(removed, 1)


class NameConventionTests(unittest.TestCase):
    def test_default_convention(self) -> None:
        entry = sha256sums.Entry(
            name=FILENAME,
            sha256=GOOD_DIGEST,
            repository="ghcr.io/zirconium-dev/zirconium/snapd",
            tag="nightly.20261001.0-arm64",
        )
        problems = sha256sums.check_names(
            {FILENAME: entry},
            name_pattern=sha256sums.DEFAULT_NAME_PATTERN,
            tag_template=sha256sums.DEFAULT_TAG_TEMPLATE,
            repository_base="ghcr.io/zirconium-dev/zirconium",
        )
        self.assertEqual(problems, [])

    def test_reports_wrong_tag(self) -> None:
        entry = sha256sums.Entry(
            name=FILENAME,
            sha256=GOOD_DIGEST,
            repository="ghcr.io/zirconium-dev/zirconium/snapd",
            tag="nightly-arm64.20261001",
        )
        problems = sha256sums.check_names(
            {FILENAME: entry},
            name_pattern=sha256sums.DEFAULT_NAME_PATTERN,
            tag_template=sha256sums.DEFAULT_TAG_TEMPLATE,
            repository_base=None,
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("nightly-arm64.20261001", problems[0])


class EndToEndTests(unittest.TestCase):
    def test_cli_merges_existing_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture.json"
            fixture.write_text(
                json.dumps(
                    {
                        "tags": {"ghcr.io/org/image/snapd": ["nightly.20261001.0-arm64"]},
                        "manifests": {
                            "ghcr.io/org/image/snapd:nightly.20261001.0-arm64": manifest_for(
                                FILENAME
                            )
                        },
                    }
                )
            )
            crane = root / "crane"
            crane.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                'db = json.load(open(os.environ["CRANE_FIXTURE"]))\n'
                'if sys.argv[1] == "ls":\n'
                '    print("\\n".join(db["tags"][sys.argv[2]]))\n'
                'elif sys.argv[1] == "manifest":\n'
                '    print(json.dumps(db["manifests"][sys.argv[2]]))\n'
                "else:\n"
                "    sys.exit(2)\n"
            )
            crane.chmod(0o755)

            existing = root / "SHA256SUMS"
            existing.write_text(f"{OTHER_DIGEST}  {OTHER_FILENAME}\n")
            output = root / "out" / "SHA256SUMS"
            mapping = root / "out" / "SHA256SUMS.json"

            with unittest.mock.patch.dict(
                os.environ, {"CRANE_FIXTURE": str(fixture)}
            ):
                result = sha256sums.main(
                    [
                        "--crane",
                        str(crane),
                        "--repository",
                        "ghcr.io/org/image/snapd",
                        "--existing",
                        str(existing),
                        "--output",
                        str(output),
                        "--mapping-output",
                        str(mapping),
                        "--filename-regex",
                        sha256sums.DEFAULT_FILENAME_REGEX,
                    ]
                )

            self.assertEqual(result, 0)
            lines = output.read_text().splitlines()
            self.assertIn(f"{GOOD_DIGEST}  {FILENAME}", lines)
            self.assertIn(f"{OTHER_DIGEST}  {OTHER_FILENAME}", lines)

            data = json.loads(mapping.read_text())
            self.assertEqual(
                data["entries"][FILENAME]["repository"], "ghcr.io/org/image/snapd"
            )
            self.assertEqual(
                data["entries"][FILENAME]["tag"], "nightly.20261001.0-arm64"
            )


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
