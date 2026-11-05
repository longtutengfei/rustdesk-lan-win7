#!/usr/bin/env python3
"""Build a Win7 SP1 x64 compatibility candidate on a modern Windows host.

Requires Flutter 3.24.5, Rust 1.75.0 with rustfmt, LLVM, FRB codegen 1.80.1,
MSVC, the isolated vcpkg software codec dependencies, and pefile 2024.8.26.
The manual GitHub workflow installs these. The build host needs Internet;
the resulting application's LAN runtime does not need Internet.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

import prepare_flutter


ROOT = Path(__file__).resolve().parents[2]
PROFILE = prepare_flutter.PROFILE
TOOLCHAIN = PROFILE["rust_toolchain"]
TARGET = PROFILE["rust_target"]
CODEGEN_VERSION = "1.80.1"
FEATURES = ["use_dasp", "flutter", "windows7"]
RUSTFLAGS = "-Ctarget-feature=+crt-static --cfg windows_slim_errors"


def cargo_command():
    # Rust 1.75's standard Windows target still supports Win7. Do not change
    # this to current stable without reviewing the standard library imports.
    return ["cargo", "+" + TOOLCHAIN, "build", "--locked", "--release",
            "--target", TARGET, "--no-default-features", "--features",
            ",".join(FEATURES), "--lib"]


def build_environment(sdk):
    env = os.environ.copy()
    # Official CI switches avoid the SDK's Azure metadata auto-detection and
    # analytics. Inherit them in codegen's Flutter/Dart subprocesses as well.
    env.update({
        "BOT": "true", "CI": "true", "FLUTTER_SUPPRESS_ANALYTICS": "true",
        "DART_SUPPRESS_ANALYTICS": "true", "RUSTUP_TOOLCHAIN": TOOLCHAIN,
        "CARGO_TARGET_DIR": str(ROOT / "target/windows7/native"),
        "CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_RUSTFLAGS": RUSTFLAGS,
        "PATH": str(sdk / "bin") + os.pathsep + env.get("PATH", ""),
    })
    for key in ("CFLAGS", "CXXFLAGS"):
        env[key] = env.get(key, "") + " /D_WIN32_WINNT=0x0601 /DWINVER=0x0601"
    # The separate CMake UI copy must package the exact Rust DLL built above.
    env["RUSTDESK_PREBUILT_LIB"] = str(
        Path(env["CARGO_TARGET_DIR"]) / TARGET / "release/librustdesk.dll")
    env.pop("RUSTDESK_PREBUILT_VIRTUAL_DISPLAY_LIB", None)
    return env


def package_version():
    source = (ROOT / "Cargo.toml").read_text(encoding="utf-8")
    package = source.split("[package]", 1)[1].split("\n[", 1)[0]
    found = re.search(r'^version\s*=\s*"([^"\n]+)"', package, re.MULTILINE)
    if not found:
        raise ValueError("No package version found in Cargo.toml")
    return found.group(1)


def verify_packaged_engine(runtime, archive):
    prepare_flutter.verify_archive(archive)
    with zipfile.ZipFile(archive) as package:
        expected = hashlib.sha256(package.read("flutter_windows.dll")).hexdigest()
    if prepare_flutter.sha256(runtime / "flutter_windows.dll") != expected:
        raise ValueError("Flutter replaced the pinned Win7 engine during the build")
    return expected


def source_identity(root=ROOT):
    identity = {"source_kind": "source-archive", "source_commit": None,
                "tracked_changes": None}
    if (root / ".git").exists():
        identity["source_kind"] = "git-worktree"
        identity["source_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        identity["tracked_changes"] = bool(subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, text=True).strip())
    paths = ["Cargo.toml", "Cargo.lock", "build.rs", "src/lib.rs", "src/core_main.rs", "src/flutter_ffi.rs",
             "src/platform/windows.rs", "flutter/windows/CMakeLists.txt",
             "flutter/windows/runner/win32_window.cpp", "scripts/windows7/profile.json",
             "scripts/windows7/pubspec.lock", "scripts/windows7/build.py",
             "scripts/windows7/prepare_flutter.py", "scripts/windows7/vcpkg.json",
             "scripts/windows7/triplets/x64-windows-static.cmake"]
    identity["source_file_sha256"] = {
        relative: prepare_flutter.sha256(root / relative)
        for relative in paths if (root / relative).is_file()
    }
    return identity


def run(arguments, env, cwd=ROOT):
    arguments = [str(value) for value in arguments]
    print("+ " + subprocess.list2cmdline(arguments), flush=True)
    subprocess.run(arguments, env=env, cwd=cwd, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flutter-sdk", required=True, type=Path)
    parser.add_argument("--llvm-path", type=Path,
                        help="LLVM installation root; defaults to LLVM_PATH or C:\\Program Files\\LLVM")
    parser.add_argument("--workspace", type=Path, default=ROOT / "target/windows7/build")
    parser.add_argument("--output", type=Path, default=ROOT / "target/windows7/dist")
    parser.add_argument("--engine-archive", type=Path,
                        default=ROOT / "target/windows7/windows-x64-release.zip")
    parser.add_argument("--system-dir", type=Path,
                        help="Optional genuine Win7 SP1 x64 System32 for stricter PE checks")
    parser.add_argument("--print-commands", action="store_true")
    args = parser.parse_args()
    if args.print_commands:
        print(subprocess.list2cmdline(cargo_command()))
        print("flutter pub get --enforce-lockfile")
        print("flutter_rust_bridge_codegen 1.80.1 (in the isolated Flutter workspace)")
        print("flutter analyze --no-pub --no-fatal-infos --no-fatal-warnings lib")
        print("flutter precache --windows; install verified pinned custom engine")
        print("flutter build windows --release --no-pub")
        print("audit_pe.py (all runtime EXE/DLL files); ZIP only after successful preflight")
        return
    if sys.platform != "win32":
        parser.error("Build from an x64 MSVC developer shell on Windows 10/11 or Windows Server")
    for name in ("VCPKG_ROOT", "VCPKG_INSTALLED_ROOT"):
        if not os.environ.get(name):
            parser.error("Set " + name + " to the isolated Win7 native dependency directory")
    if os.environ.get("RUSTFLAGS") or os.environ.get("CARGO_ENCODED_RUSTFLAGS"):
        parser.error("Unset custom Rust flags so the reviewed target's static CRT settings apply")
    sdk = args.flutter_sdk.resolve()
    flutter = sdk / "bin/flutter.bat"
    if not flutter.is_file():
        parser.error("--flutter-sdk must point to the Windows Flutter 3.24.5 SDK")
    args.workspace = args.workspace.resolve()
    args.output = args.output.resolve()
    name = "rustdesk-lan-" + package_version() + "-windows7-x64-candidate"
    archive = args.output / (name + ".zip")
    if archive.exists():
        parser.error("The package already exists; choose a new --output directory")
    env = build_environment(sdk)
    llvm = (args.llvm_path or Path(env.get("LLVM_PATH", r"C:\Program Files\LLVM"))).resolve()
    llvm_library = llvm / "bin/libclang.dll"
    if not llvm_library.is_file():
        parser.error("Pass --llvm-path to the LLVM installation containing bin/libclang.dll")
    # bindgen and ffigen use different discovery rules. Pin both to the same
    # installation instead of accidentally taking a preinstalled LLVM version.
    env["LIBCLANG_PATH"] = str(llvm_library.parent)
    run([flutter, "--version", "--machine"], env)
    prepare_flutter.verify_sdk(sdk)
    rust_version = subprocess.check_output(
        ["rustc", "+" + TOOLCHAIN, "--version"], env=env, cwd=ROOT, text=True)
    if not rust_version.startswith("rustc " + TOOLCHAIN + " "):
        parser.error("The actual Rust compiler does not match the Win7 profile")
    codegen = shutil.which("flutter_rust_bridge_codegen", path=env["PATH"])
    if not codegen:
        parser.error("Install flutter_rust_bridge_codegen 1.80.1 with --features uuid --locked")
    codegen_version = subprocess.check_output([codegen, "--version"], env=env, text=True).strip()
    if codegen_version != "flutter_rust_bridge_codegen " + CODEGEN_VERSION:
        parser.error("The installed FRB code generator must be version " + CODEGEN_VERSION)
    if args.workspace.exists():
        metadata_file = args.workspace / "ui-compatibility.json"
        if not metadata_file.is_file():
            parser.error("Existing workspace was not prepared by prepare_flutter.py")
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        if metadata.get("source_sha256") != prepare_flutter.source_hash():
            parser.error("The UI workspace is stale; choose a new --workspace directory")
        ui = args.workspace / "flutter"
    else:
        ui = prepare_flutter.prepare(args.workspace)
        metadata = json.loads((args.workspace / "ui-compatibility.json").read_text(encoding="utf-8"))
    run([flutter, "pub", "get", "--enforce-lockfile"], env, ui)
    run([codegen, "--rust-input", ROOT / "src/flutter_ffi.rs",
         "--rust-output", ROOT / "src/bridge_generated.rs",
         "--dart-output", ui / "lib/generated_bridge.dart", "--dart-root", ui,
         "--c-output", args.workspace / "bridge_generated.h", "--skip-add-mod-to-lib",
         "--llvm-path", llvm_library], env)
    run([flutter, "analyze", "--no-pub", "--no-fatal-infos", "--no-fatal-warnings", "lib"], env, ui)
    run(cargo_command(), env)
    if not Path(env["RUSTDESK_PREBUILT_LIB"]).is_file():
        raise ValueError("The native build did not produce librustdesk.dll at the expected target path")
    run([flutter, "precache", "--windows"], env)
    prepare_flutter.install_engine(sdk, args.engine_archive)
    run([flutter, "build", "windows", "--release", "--no-pub"], env, ui)
    runtime = ui / "build/windows/x64/runner/Release"
    for relative in ("rustdesk-lan.exe", "librustdesk.dll", "data/app.so", "data/icudtl.dat"):
        if not (runtime / relative).is_file():
            raise ValueError("The complete Flutter runtime is missing " + relative)
    engine_hash = verify_packaged_engine(runtime, args.engine_archive)
    shutil.copy2(ROOT / "LICENCE", runtime / "RustDesk-LAN-LICENSE.txt")
    shutil.copy2(sdk / "LICENSE", runtime / "Flutter-LICENSE.txt")
    (runtime / "WINDOWS7-TEST-NOTES.txt").write_text(
        "Windows 7 SP1 x64 compatibility candidate.\n"
        "Keep this entire folder together and run rustdesk-lan.exe.\n"
        "LAN runtime: connect directly by IP; no Internet or central server is required.\n"
        "Hardware codec / VRAM and optional virtual display/printer drivers are not bundled.\n"
        "Automated PE preflight does not replace testing on actual Windows 7.\n"
        "This package has not been runtime-validated on Windows 7 by the build workflow.\n",
        encoding="utf-8")
    args.output.mkdir(parents=True, exist_ok=True)
    audit_file = args.output / (name + "-pe-preflight.json")
    provenance = {
        "status": "compatibility-candidate", "runtime_tested_on_windows7": False,
        "minimum_os_target": PROFILE["minimum_os"], "architecture": "x64",
        "profile": PROFILE, "engine_dll_sha256": engine_hash,
        "ui_source_sha256": metadata["source_sha256"],
        **source_identity(),
        "cargo_lock_sha256": prepare_flutter.sha256(ROOT / "Cargo.lock"),
        "flutter_lock_sha256": prepare_flutter.sha256(ui / "pubspec.lock"),
        "features": FEATURES, "default_features": False,
        "target_rustflags": RUSTFLAGS,
        "hardware_codec": False, "vram": False,
    }
    provenance_file = args.output / (name + "-provenance.json")
    provenance_file.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    (runtime / "build-provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    audit_command = [sys.executable, ROOT / "scripts/windows7/audit_pe.py", runtime,
                     "--arch", "x64", "--json", audit_file]
    if args.system_dir:
        audit_command += ["--system-dir", args.system_dir, "--strict-reference"]
    run(audit_command, env)
    audit = json.loads(audit_file.read_text(encoding="utf-8"))
    provenance["pe_audit_status"] = audit["status"]
    (runtime / "build-provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(audit_file, runtime / "pe-preflight.json")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for path in sorted(runtime.rglob("*")):
            if path.is_file():
                package.write(path, Path(name) / path.relative_to(runtime))
    provenance["package"] = archive.name
    provenance["package_sha256"] = prepare_flutter.sha256(archive)
    provenance_file.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print("Compatibility candidate created: " + str(archive))
    print("PE preflight completed. Actual Windows 7 startup and LAN session tests are still required.")


if __name__ == "__main__":
    main()
