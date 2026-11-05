"""Regression tests for the compatibility build's isolation and engine guards."""

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import build
import prepare_flutter


class CompatibilityPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        (self.root / "flutter/lib").mkdir(parents=True)
        (self.root / "scripts/windows7").mkdir(parents=True)
        (self.root / "res").mkdir()
        shutil.copy2(prepare_flutter.ROOT / "flutter/pubspec.yaml", self.root / "flutter/pubspec.yaml")
        (self.root / "flutter/lib/common.dart").write_text(
            "DialogThemeData(shape: x); TabBarThemeData(labelStyle: y);", encoding="utf-8")
        for filename in ("profile.json", "prepare_flutter.py"):
            shutil.copy2(prepare_flutter.HERE / filename, self.root / "scripts/windows7" / filename)
        (self.root / "scripts/windows7/pubspec.lock").write_text("# reviewed lock fixture\n")
        (self.root / "flutter/pubspec.lock").write_text("# ordinary newer SDK lock\n")
        (self.root / "flutter/.dart_tool").mkdir()
        (self.root / "flutter/.dart_tool/package_config.json").write_text("stale cache")

    def test_preparation_changes_only_the_isolated_ui(self):
        original = (self.root / "flutter/pubspec.yaml").read_bytes()
        workspace = self.root / "target/candidate"
        ui = prepare_flutter.prepare(workspace, root=self.root)
        spec = (ui / "pubspec.yaml").read_text()
        self.assertIn('file_picker: "5.5.0"', spec)
        self.assertIn('sdk: ">=3.5.0 <3.6.0"', spec)
        self.assertIn('"ref": "85789bfe6e4cfaf4ecc00c52857467fdb7f26879"', spec)
        self.assertIn("  draggable_float_widget: ^0.1.0", spec)
        self.assertIn("  qr_code_scanner:\n    path: packages/qr_code_scanner", spec)
        self.assertEqual((ui / "lib/common.dart").read_text(),
                         "DialogTheme(shape: x); TabBarTheme(labelStyle: y);")
        self.assertFalse((ui / ".dart_tool").exists())
        self.assertEqual((ui / "pubspec.lock").read_text(), "# reviewed lock fixture\n")
        self.assertEqual((self.root / "flutter/pubspec.yaml").read_bytes(), original)
        self.assertEqual((self.root / "flutter/pubspec.lock").read_text(),
                         "# ordinary newer SDK lock\n")
        metadata = json.loads((workspace / "ui-compatibility.json").read_text())
        self.assertEqual(metadata["source_sha256"], prepare_flutter.source_hash(self.root))
        self.assertFalse(metadata["runtime_tested_on_windows7"])

    def test_refuses_overwrites_or_nested_source_destinations(self):
        existing = self.root / "target/existing"
        existing.mkdir(parents=True)
        sentinel = existing / "keep.txt"
        sentinel.write_text("user data")
        with self.assertRaisesRegex(ValueError, "never overwritten"):
            prepare_flutter.prepare(existing, root=self.root)
        self.assertEqual(sentinel.read_text(), "user data")
        with self.assertRaisesRegex(ValueError, "under target/"):
            prepare_flutter.prepare(self.root / "flutter/target/nested", root=self.root)

    def test_lock_is_required_and_source_hash_detects_updates(self):
        lock = self.root / "scripts/windows7/pubspec.lock"
        first = prepare_flutter.source_hash(self.root)
        lock.write_text("# different reviewed lock\n")
        self.assertNotEqual(first, prepare_flutter.source_hash(self.root))
        lock.unlink()
        destination = self.root / "target/unlocked"
        with self.assertRaisesRegex(ValueError, "lock is missing"):
            prepare_flutter.prepare(destination, root=self.root)
        self.assertFalse(destination.exists())


class EngineInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sdk = self.root / "flutter"
        self.destination = self.sdk / "bin/cache/artifacts/engine/windows-x64-release"
        self.destination.mkdir(parents=True)
        self.metadata = self.sdk / "bin/cache/flutter.version.json"
        self.write_sdk_metadata()
        (self.destination / "flutter_windows.dll").write_bytes(b"existing engine")

    def write_sdk_metadata(self, **changes):
        version = {
            "frameworkVersion": prepare_flutter.PROFILE["flutter_version"],
            "frameworkRevision": prepare_flutter.PROFILE["flutter_revision"],
            "dartSdkVersion": prepare_flutter.PROFILE["dart_version"],
        }
        version.update(changes)
        self.metadata.write_text(json.dumps(version))

    def make_archive(self, extra=None):
        archive = self.root / "engine.zip"
        with zipfile.ZipFile(archive, "w") as package:
            for name in ("flutter_windows.dll", "flutter_windows.dll.lib",
                         "flutter_windows.h", "gen_snapshot.exe"):
                package.writestr(name, b"test fixture only")
            if extra:
                package.writestr(extra, b"must not extract")
        return archive

    def test_rejects_wrong_sdk_before_mutating_the_engine(self):
        self.write_sdk_metadata(frameworkVersion="3.38.10")
        with self.assertRaisesRegex(ValueError, "requires Flutter 3.24.5"):
            prepare_flutter.install_engine(self.sdk, self.root / "not-downloaded.zip")
        self.assertEqual((self.destination / "flutter_windows.dll").read_bytes(), b"existing engine")
        self.write_sdk_metadata(frameworkRevision="unexpected-fork")
        with self.assertRaisesRegex(ValueError, "revision"):
            prepare_flutter.verify_sdk(self.sdk)

    def test_rejects_modified_archive_without_overwriting_the_engine(self):
        archive = self.make_archive()
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            prepare_flutter.install_engine(self.sdk, archive)
        self.assertEqual((self.destination / "flutter_windows.dll").read_bytes(), b"existing engine")

    def test_rejects_archive_path_traversal_even_if_hash_is_reviewed(self):
        archive = self.make_archive("../outside.dll")
        with patch.dict(prepare_flutter.PROFILE["engine"],
                        {"sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}):
            with self.assertRaisesRegex(ValueError, "Unexpected paths"):
                prepare_flutter.install_engine(self.sdk, archive)
        self.assertFalse((self.destination.parent / "outside.dll").exists())
        self.assertEqual((self.destination / "flutter_windows.dll").read_bytes(), b"existing engine")


class SourceArchiveTests(unittest.TestCase):
    def test_build_provenance_without_git_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "Cargo.toml"
            manifest.write_bytes(b"source archive fixture")
            with patch("build.subprocess.check_output", side_effect=AssertionError("Git must not run")):
                provenance = build.source_identity(root)
            self.assertEqual(provenance["source_kind"], "source-archive")
            self.assertIsNone(provenance["source_commit"])
            self.assertIsNone(provenance["tracked_changes"])
            self.assertEqual(provenance["source_file_sha256"]["Cargo.toml"],
                             hashlib.sha256(manifest.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
