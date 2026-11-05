"""Regression tests for the compatibility build's isolation and engine guards."""

import hashlib
import http.client
import io
import json
from pathlib import Path
import shutil
import socket
import ssl
import tempfile
import unittest
from unittest.mock import call, patch
import urllib.error
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


class EngineDownloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.archive = Path(temporary.name).resolve() / "cache/engine.zip"
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as package:
            for name in ("flutter_windows.dll", "flutter_windows.dll.lib",
                         "flutter_windows.h", "gen_snapshot.exe"):
                package.writestr(name, b"download fixture only")
        self.payload = payload.getvalue()
        profile = patch.dict(prepare_flutter.PROFILE["engine"],
                             {"sha256": hashlib.sha256(self.payload).hexdigest()})
        profile.start()
        self.addCleanup(profile.stop)
        download = patch("prepare_flutter.urllib.request.urlopen")
        self.download = download.start()
        self.addCleanup(download.stop)
        sleep = patch("prepare_flutter.time.sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def response(self, payload=None, expected_size=None):
        payload = self.payload if payload is None else payload
        response = io.BytesIO(payload)
        response.headers = {"Content-Length": str(
            len(payload) if expected_size is None else expected_size)}
        return response

    def http_error(self, code):
        return urllib.error.HTTPError(
            prepare_flutter.PROFILE["engine"]["url"], code, "fixture error", None, None)

    def assert_no_cached_or_partial_file(self):
        self.assertFalse(self.archive.exists())
        self.assertEqual(list(self.archive.parent.iterdir()), [])

    def test_transient_http_error_retries_and_caches_only_validated_bytes(self):
        for code in (408, 429, 500, 504):
            with self.subTest(code=code):
                self.download.reset_mock()
                self.sleep.reset_mock()
                self.download.side_effect = [self.http_error(code), self.response()]
                result = prepare_flutter.fetch_engine_archive(self.archive)
                self.assertEqual(result, self.archive)
                self.assertEqual(result.read_bytes(), self.payload)
                self.assertEqual(self.download.call_count, 2)
                self.sleep.assert_called_once_with(2)
                self.assertEqual(list(self.archive.parent.iterdir()), [self.archive])
                self.archive.unlink()

    def test_repeated_transient_errors_stop_after_four_attempts(self):
        self.download.side_effect = self.http_error(504)
        with self.assertRaises(urllib.error.HTTPError):
            prepare_flutter.fetch_engine_archive(self.archive)
        self.assertEqual(self.download.call_count, 4)
        self.assertEqual(self.sleep.call_args_list, [call(2), call(4), call(8)])
        self.assert_no_cached_or_partial_file()

    def test_permanent_http_errors_do_not_retry(self):
        for code in (400, 401, 403, 404):
            with self.subTest(code=code):
                self.download.reset_mock()
                self.download.side_effect = self.http_error(code)
                with self.assertRaises(urllib.error.HTTPError):
                    prepare_flutter.fetch_engine_archive(self.archive)
                self.download.assert_called_once()
                self.sleep.assert_not_called()
                self.assert_no_cached_or_partial_file()

    def test_transport_interruptions_retry(self):
        errors = [urllib.error.URLError(TimeoutError("timed out")),
                  ConnectionResetError("connection reset"),
                  http.client.IncompleteRead(b"partial", 100),
                  urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, "try again")),
                  ssl.SSLEOFError("unexpected EOF")]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.download.reset_mock()
                self.sleep.reset_mock()
                self.download.side_effect = [error, self.response()]
                prepare_flutter.fetch_engine_archive(self.archive)
                self.assertEqual(self.archive.read_bytes(), self.payload)
                self.assertEqual(self.download.call_count, 2)
                self.sleep.assert_called_once_with(2)
                self.archive.unlink()

    def test_short_response_is_retried_before_hash_validation(self):
        self.download.side_effect = [self.response(self.payload[:10], len(self.payload)),
                                     self.response()]
        prepare_flutter.fetch_engine_archive(self.archive)
        self.assertEqual(self.archive.read_bytes(), self.payload)
        self.assertEqual(self.download.call_count, 2)
        self.sleep.assert_called_once_with(2)
        self.assertEqual(list(self.archive.parent.iterdir()), [self.archive])

    def test_bad_hash_does_not_retry_or_poison_the_cache(self):
        self.download.return_value = self.response(self.payload + b"unreviewed bytes")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            prepare_flutter.fetch_engine_archive(self.archive)
        self.download.assert_called_once()
        self.sleep.assert_not_called()
        self.assert_no_cached_or_partial_file()

    def test_malformed_archive_does_not_retry_even_with_matching_hash(self):
        payload = b"not a ZIP archive"
        self.download.return_value = self.response(payload)
        with patch.dict(prepare_flutter.PROFILE["engine"],
                        {"sha256": hashlib.sha256(payload).hexdigest()}):
            with self.assertRaises(zipfile.BadZipFile):
                prepare_flutter.fetch_engine_archive(self.archive)
        self.download.assert_called_once()
        self.sleep.assert_not_called()
        self.assert_no_cached_or_partial_file()

    def test_existing_cache_is_validated_and_never_silently_replaced(self):
        self.archive.parent.mkdir()
        self.archive.write_bytes(self.payload)
        self.assertEqual(prepare_flutter.fetch_engine_archive(self.archive), self.archive)
        self.archive.write_bytes(b"modified cached file")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            prepare_flutter.fetch_engine_archive(self.archive)
        self.assertEqual(self.archive.read_bytes(), b"modified cached file")
        self.download.assert_not_called()
        self.sleep.assert_not_called()

    def test_certificate_verification_failure_does_not_retry(self):
        self.download.side_effect = urllib.error.URLError(
            ssl.SSLCertVerificationError("certificate verification failed"))
        with self.assertRaises(urllib.error.URLError):
            prepare_flutter.fetch_engine_archive(self.archive)
        self.download.assert_called_once()
        self.sleep.assert_not_called()
        self.assert_no_cached_or_partial_file()


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
