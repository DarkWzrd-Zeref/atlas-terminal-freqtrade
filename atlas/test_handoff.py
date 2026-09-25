"""Run with python -m unittest discover -s atlas -p test_handoff.py."""

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from handoff import (BundleError, MANIFEST_NAME, MAX_BUNDLE_BYTES, MAX_FILE_BYTES,
                     install_candidate, load_bundle, parse_manifest_json, validate_bundle,
                     verify_candidate)


def make_manifest(files, **updates):
    manifest = {
        "version": 1, "strategy_class": "ReviewedStrategy", "timeframe": "1h",
        "entrypoint": "ReviewedStrategy.py",
        "provenance": {"kind": "verification", "source": "Developer-authored fixture; not AI research"},
        "files": [{"path": path, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                  for path, content in files],
    }
    manifest.update(updates)
    return manifest


class StrategyBundles(unittest.TestCase):
    def setUp(self):
        self.files = [("ReviewedStrategy.py", b"raise RuntimeError('MUST NEVER EXECUTE')\n"),
                      ("helpers/__init__.py", b""), ("helpers/signals.py", b"def signal(): return 1\n"),
                      ("ReviewedStrategy.json", b'{"params": {"buy": {"period": 21}}}\n'),
                      ("calibration.csv", b"a,b\n1,2\n"), ("weights.bin", b"\x00\x01\xff\xfe")]
        self.manifest = make_manifest(self.files)

    def test_install_preserves_real_source_support_and_parameters_without_execution(self):
        bundle = validate_bundle(self.manifest, self.files)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch("builtins.compile", side_effect=AssertionError("Source must remain inert")):
                result = install_candidate(root, bundle, role="lab", reviewed_sha256=bundle.sha256)
            self.assertEqual(result.strategy_path, result.directory)
            self.assertEqual((result.strategy_class, result.timeframe), ("ReviewedStrategy", "1h"))
            for path, content in self.files:
                self.assertEqual((result.directory / path).read_bytes(), content)
            reread = verify_candidate(result.directory, expected_sha256=bundle.sha256)
            self.assertEqual(reread, bundle)
            self.assertEqual(install_candidate(root, bundle, role="lab", reviewed_sha256=bundle.sha256), result)
            self.assertEqual(len(list(root.iterdir())), 1)

    def test_order_independent_digest_binds_metadata_and_all_file_hashes(self):
        original = validate_bundle(self.manifest, self.files)
        reverse = copy.deepcopy(self.manifest)
        reverse["files"].reverse()
        self.assertEqual(validate_bundle(reverse, reversed(self.files)).sha256, original.sha256)
        for field, value in (("timeframe", "4h"), ("strategy_class", "OtherStrategy"),
                             ("provenance", {"kind": "manual", "source": "Different origin"})):
            manifest = copy.deepcopy(self.manifest)
            manifest[field] = value
            self.assertNotEqual(validate_bundle(manifest, self.files).sha256, original.sha256)
        changed_files = self.files[:-1] + [("weights.bin", b"different bytes")]
        self.assertNotEqual(validate_bundle(make_manifest(changed_files), changed_files).sha256, original.sha256)
        mutated_view = original.manifest
        mutated_view["strategy_class"] = "Changed"
        self.assertEqual(original.manifest["strategy_class"], "ReviewedStrategy")

    def test_review_and_role_fail_before_writing(self):
        bundle = validate_bundle(self.manifest, self.files)
        with tempfile.TemporaryDirectory() as folder:
            for role, digest in (("paper", bundle.sha256), ("lab", "0" * 64), ("lab", "invalid\u2603")):
                with self.subTest(role=role), self.assertRaises(BundleError):
                    install_candidate(Path(folder), bundle, role=role, reviewed_sha256=digest)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_path_traversal_windows_devices_and_ambiguous_paths_rejected(self):
        for path in ("../escape.py", "/escape.py", "C:/escape.py", "x\\escape.py", "x/./a.py",
                     "x//a.py", "x/../a.py", "x/CON.py", "nul", "a.py:secret", "x./a.py",
                     "x /a.py", "x\n/a.py", MANIFEST_NAME, "e\u0301.py", "x\ud800.py", "x/" * 17 + "a.py"):
            with self.subTest(path=path), self.assertRaises(BundleError):
                files = self.files + [(path, b"content")]
                validate_bundle(make_manifest(files), files)

    def test_duplicate_case_collision_parent_collision_and_extra_missing_files(self):
        variants = [self.files + [self.files[0]], self.files + [("reviewedstrategy.PY", b"other")],
                    self.files + [("helpers", b"file")]]
        for files in variants:
            with self.subTest(paths=[p for p, _ in files]), self.assertRaises(BundleError):
                validate_bundle(make_manifest(files), files)
        for files in (self.files + [self.files[0]], self.files[:-1], self.files + [("extra.py", b"")]):
            with self.assertRaises(BundleError):
                validate_bundle(self.manifest, files)

    def test_size_limits_before_reads_and_content_hash_mismatch(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["files"][0]["size"] = MAX_FILE_BYTES + 1
        with self.assertRaises(BundleError):
            validate_bundle(manifest, ())
        count = MAX_BUNDLE_BYTES // MAX_FILE_BYTES + 1
        manifest["files"] = [{"path": f"file{n}.py", "size": MAX_FILE_BYTES, "sha256": "0" * 64}
                             for n in range(count)]
        with self.assertRaises(BundleError):
            validate_bundle(manifest, ())
        altered = [(self.files[0][0], b"x" * len(self.files[0][1])), *self.files[1:]]
        with self.assertRaises(BundleError):
            validate_bundle(self.manifest, altered)
        with self.assertRaises(BundleError):
            validate_bundle(self.manifest, [(self.files[0][0], "not bytes"), *self.files[1:]])

    def test_archives_rejected_by_name_or_signature(self):
        for name, data in (("bundle.zip", b"not even a valid archive"), ("model.npz", b""),
                           ("innocent.dat", b"PK\x03\x04hidden"), ("hidden.py", b"\x1f\x8bgzip"),
                           ("tar.dat", b"\x00" * 257 + b"ustar\x00")):
            with self.subTest(name=name), self.assertRaises(BundleError):
                files = self.files + [(name, data)]
                validate_bundle(make_manifest(files), files)

    def test_schema_rejects_invalid_metadata_and_absent_entrypoint(self):
        updates = ({"version": True}, {"version": 2}, {"timeframe": "0h"}, {"strategy_class": "A:base64"},
                   {"strategy_class": "class"}, {"entrypoint": "absent.py"}, {"entrypoint": "calibration.csv"},
                   {"provenance": {}}, {"files": []}, {"unknown": "not allowed"})
        for update in updates:
            with self.subTest(update=update), self.assertRaises(BundleError):
                validate_bundle(dict(self.manifest, **update), self.files)

    def test_json_parser_rejects_duplicate_keys_bad_encoding_and_deep_nesting(self):
        self.assertEqual(parse_manifest_json(json.dumps(self.manifest).encode())["strategy_class"],
                         "ReviewedStrategy")
        for payload in (b'{"version":1,"version":1}', b'\xff', b'[' * 2000 + b']' * 2000,
                        b'{"provenance":{"kind":"a","kind":"b"}}'):
            with self.subTest(payload=payload[:50]), self.assertRaises(BundleError):
                parse_manifest_json(payload)

    def test_directory_loader_rejects_undeclared_files_and_keeps_nested_layout(self):
        files = [("package/ReviewedStrategy.py", self.files[0][1]), ("package/helpers.py", b"value = 1")]
        manifest = make_manifest(files, entrypoint="package/ReviewedStrategy.py")
        with tempfile.TemporaryDirectory() as folder:
            source, destination = Path(folder) / "source", Path(folder) / "lab"
            source.mkdir()
            destination.mkdir()
            for path, content in files:
                target = source / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            bundle = load_bundle(manifest, source)
            result = install_candidate(destination, bundle, role="lab", reviewed_sha256=bundle.sha256)
            self.assertEqual(result.strategy_path, result.directory / "package")
            (source / "undeclared.txt").write_text("do not silently ignore")
            with self.assertRaises(BundleError):
                load_bundle(manifest, source)

    def test_source_size_mismatch_rejected_before_file_read(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "ReviewedStrategy.py").write_text("wrong size")
            with patch("handoff._read_regular", side_effect=AssertionError("Must reject before reading")):
                with self.assertRaises(BundleError):
                    load_bundle(self.manifest, Path(folder))

    def test_tampering_never_overwrites_existing_candidate(self):
        bundle = validate_bundle(self.manifest, self.files)
        with tempfile.TemporaryDirectory() as folder:
            result = install_candidate(Path(folder), bundle, role="lab", reviewed_sha256=bundle.sha256)
            target = result.directory / self.files[0][0]
            target.write_text("changed")
            with self.assertRaises(BundleError):
                verify_candidate(result.directory, expected_sha256=bundle.sha256)
            with self.assertRaises(BundleError):
                install_candidate(Path(folder), bundle, role="lab", reviewed_sha256=bundle.sha256)
            self.assertEqual(target.read_text(), "changed")

    def test_atomic_failure_leaves_no_candidate_or_partial_staging(self):
        bundle = validate_bundle(self.manifest, self.files)
        with tempfile.TemporaryDirectory() as folder, patch.object(Path, "rename", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                install_candidate(Path(folder), bundle, role="lab", reviewed_sha256=bundle.sha256)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_symlinked_source_or_parent_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            target = root / "target.py"
            target.write_text("external")
            try:
                (source / "ReviewedStrategy.py").symlink_to(target)
                (root / "linked").symlink_to(source, target_is_directory=True)
            except OSError:
                self.skipTest("Symlink creation requires privileges on this platform")
            with self.assertRaises(BundleError):
                load_bundle(self.manifest, source)
            with self.assertRaises(BundleError):
                load_bundle(self.manifest, root / "linked")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO files are POSIX only")
    def test_special_files_rejected_without_blocking_read(self):
        with tempfile.TemporaryDirectory() as folder:
            os.mkfifo(Path(folder) / "ReviewedStrategy.py")
            with self.assertRaises(BundleError):
                load_bundle(self.manifest, Path(folder))


if __name__ == "__main__":
    unittest.main()
