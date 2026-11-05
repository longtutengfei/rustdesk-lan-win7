#!/usr/bin/env python3
"""Prepare an isolated Flutter 3.24.5 UI and install its pinned Win7 engine.

The ordinary Flutter source and lock file remain untouched. A Win7 build must
use this SDK/engine pair; swapping only flutter_windows.dll into a newer AOT
build is not ABI compatible.
"""

import argparse
import hashlib
import http.client
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import ssl
import tempfile
import time
import urllib.error
import urllib.request
import zipfile


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROFILE = json.loads((HERE / "profile.json").read_text(encoding="utf-8"))
ENGINE_DOWNLOAD_ATTEMPTS = 4
IGNORED = {
    ".dart_tool", ".git", ".plugin_symlinks", ".symlinks", "build", "ephemeral",
    "Pods", ".flutter-plugins", ".flutter-plugins-dependencies",
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_hash(root=ROOT):
    """Include the compatibility profile so an old workspace cannot be reused."""
    digest = hashlib.sha256()
    paths = list((root / "flutter").rglob("*"))
    paths += [root / "res/icon.png", root / "res/mac-icon.png",
              root / "scripts/windows7/profile.json",
              root / "scripts/windows7/prepare_flutter.py",
              root / "scripts/windows7/pubspec.lock"]
    for path in sorted(paths):
        relative = path.relative_to(root)
        if not path.is_file() or any(part in IGNORED for part in relative.parts):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def replace_yaml_entries(source, section, replacements, allow_add=False):
    """Edit only top-level entries in one known two-space-indented section.

    JSON values are valid YAML flow values, so no third-party YAML parser is
    required on the Windows build host.
    """
    lines = source.splitlines(keepends=True)
    headings = [i for i, line in enumerate(lines) if line.rstrip() == section + ":"]
    if len(headings) != 1:
        raise ValueError("Expected one pubspec section: " + section)
    start = headings[0] + 1
    for key, value in replacements.items():
        end = next((i for i in range(start, len(lines))
                    if re.match(r"^[A-Za-z_]", lines[i])), len(lines))
        matches = [i for i in range(start, end)
                   if re.match(r"^  " + re.escape(key) + r":", lines[i])]
        if len(matches) > 1 or (not matches and not allow_add):
            raise ValueError("Expected one pubspec entry: " + section + "." + key)
        rendered = "  " + key + ": " + json.dumps(value, ensure_ascii=False) + "\n"
        if not matches:
            lines.insert(start, rendered)
            continue
        first = matches[0]
        last = first + 1
        while last < end and lines[last].startswith("    "):
            last += 1
        lines[first:last] = [rendered]
    return "".join(lines)


def compatible_pubspec(source):
    source = replace_yaml_entries(source, "environment", {
        "sdk": ">=3.5.0 <3.6.0", "flutter": ">=3.24.5 <3.25.0",
    }, allow_add=True)
    for section in ("dependencies", "dependency_overrides"):
        source = replace_yaml_entries(source, section, PROFILE[section])
    return source


def prepare(destination, root=ROOT, require_lock=True):
    destination = Path(destination).resolve()
    root = root.resolve()
    if destination.exists():
        raise ValueError("Choose a new build directory; an existing workspace is never overwritten")
    if destination == root or (root in destination.parents and
                               destination.relative_to(root).parts[0] != "target"):
        raise ValueError("The compatibility copy must be under target/ or outside the repository")
    lock = root / "scripts/windows7/pubspec.lock"
    if require_lock and not lock.is_file():
        raise ValueError("The reviewed Windows 7 pubspec.lock is missing")
    flutter = destination / "flutter"
    shutil.copytree(root / "flutter", flutter,
                    ignore=shutil.ignore_patterns(*sorted(IGNORED)))
    pubspec = flutter / "pubspec.yaml"
    pubspec.write_text(compatible_pubspec(pubspec.read_text(encoding="utf-8")),
                       encoding="utf-8")
    if lock.is_file():
        shutil.copy2(lock, flutter / "pubspec.lock")
    else:
        (flutter / "pubspec.lock").unlink(missing_ok=True)
    common = flutter / "lib/common.dart"
    original = common.read_text(encoding="utf-8")
    compatible = original.replace("DialogThemeData(", "DialogTheme(")
    compatible = compatible.replace("TabBarThemeData(", "TabBarTheme(")
    common.write_text(compatible, encoding="utf-8")
    # Launcher-icon configuration has paths outside flutter/.
    (destination / "res").mkdir()
    for name in ("icon.png", "mac-icon.png"):
        source = root / "res" / name
        if source.is_file():
            shutil.copy2(source, destination / "res" / name)
    metadata = {
        "flutter_version": PROFILE["flutter_version"],
        "dart_version": PROFILE["dart_version"],
        "source_sha256": source_hash(root),
        "backported_files": ["lib/common.dart"] if compatible != original else [],
        "runtime_tested_on_windows7": False,
    }
    (destination / "ui-compatibility.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return flutter


def verify_sdk(sdk):
    sdk = Path(sdk).resolve()
    metadata = sdk / "bin/cache/flutter.version.json"
    if not metadata.is_file():
        raise ValueError("Initialize the Flutter SDK with flutter --version first")
    version = json.loads(metadata.read_text(encoding="utf-8"))
    if version.get("frameworkVersion") != PROFILE["flutter_version"]:
        raise ValueError("This engine requires Flutter " + PROFILE["flutter_version"])
    if version.get("frameworkRevision") != PROFILE["flutter_revision"]:
        raise ValueError("The Flutter SDK revision does not match the reviewed engine profile")
    if not version.get("dartSdkVersion", "").startswith(PROFILE["dart_version"]):
        raise ValueError("The Dart SDK does not match the reviewed engine profile")
    return sdk


def verify_archive(archive):
    archive = Path(archive)
    if sha256(archive) != PROFILE["engine"]["sha256"]:
        raise ValueError("Custom engine SHA-256 mismatch; refusing to install it")
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        required = {"flutter_windows.dll", "flutter_windows.dll.lib", "flutter_windows.h",
                    "gen_snapshot.exe"}
        if not required.issubset(names):
            raise ValueError("The custom engine archive is missing required files")
        if len(set(names)) != len(names) or any(
                name in (".", "..") or PurePosixPath(name).name != name or "\\" in name
                for name in names):
            raise ValueError("Unexpected paths or duplicate files in the custom engine archive")
    return archive


def transient_download_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (408, 429) or 500 <= error.code < 600
    if isinstance(error, urllib.error.ContentTooShortError):
        return True
    if isinstance(error, urllib.error.URLError):
        error = error.reason
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    return isinstance(error, (TimeoutError, ConnectionError,
                              http.client.IncompleteRead, ssl.SSLEOFError))


def fetch_engine_archive(archive):
    """Cache only the reviewed archive; never retry a hash or archive failure."""
    archive = Path(archive).resolve()
    if archive.is_file():
        return verify_archive(archive)
    archive.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, ENGINE_DOWNLOAD_ATTEMPTS + 1):
        with tempfile.NamedTemporaryFile(dir=archive.parent, delete=False) as temporary:
            download = Path(temporary.name)
        try:
            with urllib.request.urlopen(PROFILE["engine"]["url"], timeout=60) as response:
                with download.open("wb") as output:
                    shutil.copyfileobj(response, output)
                    received = output.tell()
                expected = response.headers.get("Content-Length")
                if expected is not None and received < int(expected):
                    raise urllib.error.ContentTooShortError(
                        f"Engine download received {received} of {expected} bytes", None)
            verify_archive(download)
            download.replace(archive)
            return archive
        except (OSError, http.client.IncompleteRead) as error:
            if attempt == ENGINE_DOWNLOAD_ATTEMPTS or not transient_download_error(error):
                raise
            delay = 2 ** attempt
            print(f"Engine download attempt {attempt}/{ENGINE_DOWNLOAD_ATTEMPTS} "
                  f"failed: {error}; retrying in {delay}s", flush=True)
        finally:
            download.unlink(missing_ok=True)
        time.sleep(delay)


def install_engine(sdk, archive):
    sdk = verify_sdk(sdk)
    destination = sdk / "bin/cache/artifacts/engine/windows-x64-release"
    if not destination.is_dir():
        raise ValueError("Run flutter precache --windows before installing the custom engine")
    archive = fetch_engine_archive(archive)
    with zipfile.ZipFile(archive) as package:
        package.extractall(destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--prefetch-engine", action="store_true",
                        help="Download and validate the pinned archive without changing the SDK")
    parser.add_argument("--install-engine", type=Path, metavar="FLUTTER_SDK")
    parser.add_argument("--engine-archive", type=Path,
                        default=ROOT / "target/windows7/windows-x64-release.zip")
    args = parser.parse_args()
    if not args.workspace and not args.prefetch_engine and not args.install_engine:
        parser.error("Specify --workspace, --prefetch-engine or --install-engine")
    if args.workspace:
        print(prepare(args.workspace))
    if args.prefetch_engine:
        print(fetch_engine_archive(args.engine_archive))
    if args.install_engine:
        print(install_engine(args.install_engine, args.engine_archive))


if __name__ == "__main__":
    main()
