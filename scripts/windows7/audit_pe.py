#!/usr/bin/env python3
"""Audit a Windows package without executing it (requires pefile==2024.8.26).

Without --system-dir this is deliberately only a preflight: known newer APIs
are rejected, but unlisted system exports are NOT assumed to be available.
With an actual Windows 7 SP1 System32 copy, named/ordinal imports and export
forwarders are checked against those files. Neither mode tests runtime paths,
GetProcAddress/LoadLibrary calls, graphics drivers, or actual Windows startup.

Exit codes: 0 = no detected errors (read status/limitations); 1 = audit errors;
2 = invalid input, missing parser, or invalid Windows reference directory.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys

try:
    import pefile
except ImportError:
    pefile = None


MACHINES = {"x64": 0x8664, "x86": 0x14C}
WIN7_VERSION = (6, 1)

# This list recognizes common OS DLL names; it is NOT an export allowlist or a
# proof of compatibility. Real export validation requires --system-dir.
SYSTEM_DLLS = set("""
advapi32.dll avicap32.dll avrt.dll bcrypt.dll bcryptprimitives.dll cabinet.dll cfgmgr32.dll
comctl32.dll comdlg32.dll credui.dll crypt32.dll cryptbase.dll cryptnet.dll
cryptsp.dll d2d1.dll d3d9.dll d3d10.dll d3d10_1.dll d3d11.dll dbghelp.dll
dciman32.dll dnsapi.dll dsound.dll dwmapi.dll dwrite.dll dxgi.dll dxva2.dll
gdi32.dll glu32.dll hid.dll imagehlp.dll imm32.dll iphlpapi.dll kernel32.dll
kernelbase.dll mf.dll mfplat.dll mfreadwrite.dll mfuuid.dll mmdevapi.dll
mpr.dll msacm32.dll msasn1.dll mscms.dll msimg32.dll msvcrt.dll ncrypt.dll
netapi32.dll normaliz.dll nsi.dll ntdll.dll ole32.dll oleacc.dll oleaut32.dll
opengl32.dll pdh.dll powrprof.dll propsys.dll psapi.dll rasapi32.dll
rpcrt4.dll secur32.dll sechost.dll sensapi.dll setupapi.dll shell32.dll
shlwapi.dll slc.dll spoolss.dll sspicli.dll sxs.dll uiautomationcore.dll
urlmon.dll user32.dll userenv.dll usp10.dll uxtheme.dll version.dll
wevtapi.dll wer.dll winhttp.dll wininet.dll winmm.dll winspool.drv
wintrust.dll wlanapi.dll wldap32.dll ws2_32.dll wsock32.dll wtsapi32.dll
""".split())

# Windows KnownDLLs cannot be replaced by copying a same-named DLL alongside
# the EXE. Only the core names relevant to this package are needed here.
KNOWN_DLLS = set("""
advapi32.dll gdi32.dll kernel32.dll kernelbase.dll msvcrt.dll ntdll.dll
ole32.dll oleaut32.dll rpcrt4.dll sechost.dll shell32.dll shlwapi.dll
user32.dll ws2_32.dll
""".split())

# Deliberately small, documented blocker list. Win7 backported APIs such as
# AddDllDirectory/SetDefaultDllDirectories are NOT unconditionally rejected.
# SetWaitableTimerEx and bcryptprimitives.dll already exist on Win7; only
# ProcessPrng is a blocker, not the bcryptprimitives DLL name itself.
# https://learn.microsoft.com/windows/win32/api/synchapi/nf-synchapi-waitonaddress
# https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-getdpiforwindow
# https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-setthreaddescription
# https://learn.microsoft.com/windows/win32/api/synchapi/nf-synchapi-setwaitabletimerex
# https://learn.microsoft.com/windows/win32/seccng/processprng
NEW_KERNEL_APIS = set("""
WaitOnAddress WakeByAddressAll WakeByAddressSingle
GetSystemTimePreciseAsFileTime GetCurrentThreadStackLimits
GetOverlappedResultEx GetProcessInformation SetProcessInformation
GetThreadInformation SetThreadInformation GetThreadDescription
SetThreadDescription IsWow64Process2 PrefetchVirtualMemory
DiscardVirtualMemory VirtualAlloc2 VirtualAlloc2FromApp
GetSystemCpuSetInformation GetThreadSelectedCpuSets SetThreadSelectedCpuSets
GetPackageFamilyName GetCurrentPackageFamilyName GetCurrentPackageFullName
GetTempPath2A GetTempPath2W
""".split())
NEW_USER_APIS = set("""
GetDpiForWindow GetDpiForSystem SetProcessDpiAwarenessContext
SetThreadDpiAwarenessContext GetThreadDpiAwarenessContext
GetWindowDpiAwarenessContext GetAwarenessFromDpiAwarenessContext
AreDpiAwarenessContextsEqual EnableNonClientDpiScaling
AdjustWindowRectExForDpi GetSystemMetricsForDpi SystemParametersInfoForDpi
GetPointerInfo GetPointerTouchInfo GetPointerPenInfo RegisterPointerInputTarget
""".split())
NEW_DLLS = {
    "combase.dll", "dcomp.dll", "shcore.dll",
    "api-ms-win-core-path-l1-1-0.dll",
}


class AuditInputError(Exception):
    pass


def dll_name(value: str) -> str:
    value = value.lower()
    if not value or "/" in value or "\\" in value or value in {".", ".."}:
        raise AuditInputError(f"Unsupported DLL name: {value!r}")
    return value if "." in value else value + ".dll"


def decoded(value: bytes) -> str:
    try:
        return value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AuditInputError("Non-ASCII PE import/export name") from exc


@dataclass(frozen=True)
class Import:
    dll: str
    symbols: tuple[str | int, ...]
    delayed: bool = False


@dataclass
class Image:
    path: Path
    machine: int
    os_version: tuple[int, int]
    subsystem_version: tuple[int, int]
    file_version: tuple[int, int, int, int] | None
    imports: list[Import]
    exports: dict[str | int, str | None]


def read_image(path: Path) -> Image:
    if pefile is None:
        raise AuditInputError("Install the parser: python -m pip install pefile==2024.8.26")
    try:
        with pefile.PE(str(path), fast_load=True) as pe:
            pe.parse_data_directories(directories=[0, 1, 2, 13])
            for index, attribute in (
                (0, "DIRECTORY_ENTRY_EXPORT"), (1, "DIRECTORY_ENTRY_IMPORT"),
                (13, "DIRECTORY_ENTRY_DELAY_IMPORT"),
            ):
                directories = pe.OPTIONAL_HEADER.DATA_DIRECTORY
                if (index < len(directories) and directories[index].VirtualAddress
                        and not hasattr(pe, attribute)):
                    raise AuditInputError(f"{path}: malformed PE directory {index}")
            imports = []
            for attribute, delayed in (("DIRECTORY_ENTRY_IMPORT", False),
                                       ("DIRECTORY_ENTRY_DELAY_IMPORT", True)):
                for entry in getattr(pe, attribute, []):
                    symbols = tuple(decoded(item.name) if item.name is not None
                                    else int(item.ordinal) for item in entry.imports)
                    imports.append(Import(dll_name(decoded(entry.dll)), symbols, delayed))
            exports = {}
            for item in getattr(getattr(pe, "DIRECTORY_ENTRY_EXPORT", None), "symbols", []):
                if not item.address:
                    continue  # A zero EAT slot does not export a callable function.
                forwarder = decoded(item.forwarder) if item.forwarder else None
                exports[int(item.ordinal)] = forwarder
                if item.name is not None:
                    exports[decoded(item.name)] = forwarder
            version = None
            if getattr(pe, "VS_FIXEDFILEINFO", None):
                v = pe.VS_FIXEDFILEINFO[0]
                version = (v.FileVersionMS >> 16, v.FileVersionMS & 0xFFFF,
                           v.FileVersionLS >> 16, v.FileVersionLS & 0xFFFF)
            return Image(
                path.resolve(), pe.FILE_HEADER.Machine,
                (pe.OPTIONAL_HEADER.MajorOperatingSystemVersion,
                 pe.OPTIONAL_HEADER.MinorOperatingSystemVersion),
                (pe.OPTIONAL_HEADER.MajorSubsystemVersion,
                 pe.OPTIONAL_HEADER.MinorSubsystemVersion),
                version, imports, exports,
            )
    except (OSError, pefile.PEFormatError, ValueError, TypeError) as exc:
        raise AuditInputError(f"Cannot parse {path}: {exc}") from exc


def newer_api(module: str, symbol: str | int) -> bool:
    if not isinstance(symbol, str):
        return False
    if module in {"kernel32.dll", "kernelbase.dll"} or module.startswith("api-ms-win-core-"):
        return symbol in NEW_KERNEL_APIS
    if module == "user32.dll" or module.startswith("api-ms-win-ntuser-"):
        return symbol in NEW_USER_APIS
    return module == "bcryptprimitives.dll" and symbol == "ProcessPrng"


def symbol_name(symbol: str | int) -> str:
    return symbol if isinstance(symbol, str) else f"#{symbol}"


def audit(package: Path, arch: str = "x64", system_dir: Path | None = None,
          api_sets: dict[str, str] | None = None, strict_delay: bool = False) -> dict:
    package = package.resolve()
    if not package.is_dir():
        raise AuditInputError(f"Package directory does not exist: {package}")
    paths = sorted(p for p in package.rglob("*")
                   if p.is_file() and p.suffix.lower() in {".exe", ".dll"})
    if not paths:
        raise AuditInputError(f"No EXE or DLL files in {package}")
    expected_machine = MACHINES[arch]
    issues = []

    def issue(severity: str, code: str, path: Path, detail: str) -> None:
        record = {"severity": severity, "code": code,
                  "file": str(path), "detail": detail}
        if record not in issues:
            issues.append(record)

    images = {}
    packaged = {}
    for path in paths:
        path = path.resolve()
        key = (path.parent, path.name.lower())
        if key in packaged:
            issue("error", "case-collision", path, "Duplicate case-insensitive DLL/EXE name")
        packaged[key] = path
        try:
            images[path] = read_image(path)
        except AuditInputError as exc:
            issue("error", "invalid-pe", path, str(exc))

    system = {}
    reference = None
    if system_dir is not None:
        system_dir = system_dir.resolve()
        if not system_dir.is_dir():
            raise AuditInputError(f"System reference directory does not exist: {system_dir}")
        for path in system_dir.iterdir():
            if path.is_file():
                name = path.name.lower()
                if name in system:
                    raise AuditInputError(f"Case-colliding system reference: {name}")
                system[name] = path
        kernel_path = system.get("kernel32.dll")
        if kernel_path is None:
            raise AuditInputError("--system-dir must contain Windows 7 SP1 kernel32.dll")
        kernel = read_image(kernel_path)
        if kernel.machine != expected_machine:
            raise AuditInputError("System reference architecture does not match --arch")
        if not kernel.file_version or kernel.file_version[:3] != (6, 1, 7601):
            raise AuditInputError(
                "Reference kernel32.dll must have file version 6.1.7601.* (Windows 7 SP1); "
                f"got {kernel.file_version!r}. Do not use a Windows 10/11 System32 directory."
            )
        images[kernel.path] = kernel
        reference = {"directory": str(system_dir), "kernel32_file_version": kernel.file_version,
                     "kernel32_sha256": hashlib.sha256(kernel_path.read_bytes()).hexdigest()}

    mappings = {dll_name(k): dll_name(v) for k, v in (api_sets or {}).items()}
    unverified_system = set()
    resolved_imports = []

    def module_path(module: str, importer: Path) -> tuple[Path | None, str]:
        mapped = mappings.get(module, module)
        # A bundled fake kernel32/user32 cannot replace a Windows KnownDLL.
        if mapped not in KNOWN_DLLS:
            for directory in (importer.parent, package):
                candidate = packaged.get((directory, mapped))
                if candidate is not None:
                    return candidate, "package"
        if mapped in system:
            return system[mapped].resolve(), "reference"
        if system_dir is not None:
            return None, "unresolved-reference"
        if mapped in NEW_DLLS or mapped.startswith("api-ms-win-core-winrt-"):
            return None, "post-win7-dll"
        if mapped in SYSTEM_DLLS or mapped.startswith(("api-ms-", "ext-ms-")):
            return None, "unverified-system"
        return None, "missing-dll"

    def resolve_export(module: str, symbol: str | int, importer: Path,
                       seen: frozenset = frozenset()) -> tuple[str, str]:
        target, origin = module_path(module, importer)
        if target is None:
            if newer_api(module, symbol):
                return "post-win7-api", f"{module}!{symbol_name(symbol)} requires newer Windows"
            if origin == "unverified-system":
                unverified_system.add(module)
                return "unverified-system", f"No Win7 export reference for {module}"
            return origin, f"Cannot resolve {module}!{symbol_name(symbol)} ({origin})"
        if target not in images:
            try:
                images[target] = read_image(target)
            except AuditInputError as exc:
                return "invalid-dependency", str(exc)
        provider = images[target]
        if provider.machine != expected_machine:
            return "dependency-architecture", f"{target} has machine {provider.machine:#x}"
        identity = (target, symbol)
        if identity in seen or len(seen) >= 32:
            return "forwarder-cycle", f"Export-forwarder cycle at {target.name}!{symbol_name(symbol)}"
        if symbol not in provider.exports:
            return "missing-export", f"{target} does not export {symbol_name(symbol)}"
        forwarder = provider.exports[symbol]
        if forwarder:
            destination, separator, name = forwarder.rpartition(".")
            if not separator or not name:
                return "invalid-forwarder", f"{target}: invalid forwarder {forwarder!r}"
            try:
                next_symbol = int(name[1:]) if name.startswith("#") else name
                next_module = dll_name(destination)
            except (AuditInputError, ValueError) as exc:
                return "invalid-forwarder", f"{target}: {exc}"
            return resolve_export(next_module, next_symbol, target, seen | {identity})
        return "resolved", str(target)

    file_reports = []
    for path in paths:
        path = path.resolve()
        image = images.get(path)
        if image is None:
            continue
        if image.machine != expected_machine:
            issue("error", "architecture", path,
                  f"Expected {arch} ({expected_machine:#x}), found {image.machine:#x}")
        for kind, value in (("os-version", image.os_version),
                            ("subsystem-version", image.subsystem_version)):
            if value > WIN7_VERSION:
                issue("error", kind, path, f"PE requires {value[0]}.{value[1]}, above Windows 7 (6.1)")
        if path.name.lower() in KNOWN_DLLS:
            issue("error", "bundled-known-dll", path,
                  "Do not bundle a Windows KnownDLL as a compatibility shim")
        file_reports.append({
            "file": str(path.relative_to(package)), "machine": f"{image.machine:#x}",
            "os_version": image.os_version, "subsystem_version": image.subsystem_version,
            "normal_import_dlls": [i.dll for i in image.imports if not i.delayed],
            "delay_import_dlls": [i.dll for i in image.imports if i.delayed],
        })
        for imported in image.imports:
            # A delay import may be guarded at runtime. Preserve it in the
            # report and require review, rather than calling it a startup fault.
            severity = "error" if strict_delay or not imported.delayed else "warning"
            target, origin = module_path(imported.dll, path)
            if not imported.symbols:
                issue(severity, "empty-import", path, f"No parsed symbols for {imported.dll}")
            for symbol in imported.symbols:
                status, detail = resolve_export(imported.dll, symbol, path)
                resolved_imports.append({"file": str(path.relative_to(package)),
                                         "dll": imported.dll, "symbol": symbol_name(symbol),
                                         "delayed": imported.delayed, "status": status,
                                         "provider": str(target) if target else None})
                if status not in {"resolved", "unverified-system"}:
                    issue(severity, status, path,
                          ("Delay import: " if imported.delayed else "Import: ") + detail)
            if target is None and origin == "unverified-system" and imported.dll.startswith(("api-ms-", "ext-ms-")):
                issue("warning", "unverified-api-set", path,
                      f"{imported.dll}: validate against the Win7 API-set mapping/reference")

    errors = sum(item["severity"] == "error" for item in issues)
    return {
        "schema_version": 1,
        "status": "blocked" if errors else ("reference-checked" if reference else "preflight-only"),
        "package": str(package), "architecture": arch, "reference": reference,
        "files": file_reports, "imports": resolved_imports, "issues": issues,
        "error_count": errors,
        "warning_count": sum(item["severity"] == "warning" for item in issues),
        "unverified_system_dlls": sorted(unverified_system),
        "runtime_validation_required": True,
        "limitations": [
            "No binaries were executed. A clean report is not a Windows 7 runtime certification.",
            "Without a system reference, only known newer APIs are rejected; other OS exports remain unverified.",
            "LoadLibrary/GetProcAddress, driver requirements, CPU instructions, and API semantics are not checked.",
            "Delay imports may be guarded at runtime; unresolved delay imports require review.",
            "DLL lookup models the importing directory and package root, not arbitrary runtime PATH/search changes.",
            "Side-by-side manifests, activation contexts, and external redistributable installation are not modeled.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="Unpacked runtime package directory")
    parser.add_argument("--arch", choices=sorted(MACHINES), default="x64")
    parser.add_argument("--json", dest="json_path", type=Path, help="Save the complete audit report")
    parser.add_argument("--system-dir", type=Path, help="Actual Windows 7 SP1 System32 (matching architecture)")
    parser.add_argument("--strict-reference", action="store_true", help="Refuse preflight-only mode")
    parser.add_argument("--api-set-map", type=Path, help="Optional JSON contract-DLL to host-DLL mapping from target Windows")
    parser.add_argument("--strict-delay-imports", action="store_true", help="Treat unresolved delay imports as errors")
    args = parser.parse_args(argv)
    try:
        if args.strict_reference and args.system_dir is None:
            raise AuditInputError("--strict-reference requires --system-dir")
        mappings = None
        if args.api_set_map is not None:
            mappings = json.loads(args.api_set_map.read_text(encoding="utf-8"))
            if not isinstance(mappings, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in mappings.items()):
                raise AuditInputError("--api-set-map must be a JSON object of DLL-name strings")
        report = audit(args.package, args.arch, args.system_dir, mappings, args.strict_delay_imports)
        if args.json_path:
            args.json_path.parent.mkdir(parents=True, exist_ok=True)
            args.json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"{report['status']}: {len(report['files'])} PE files, "
              f"{report['error_count']} errors, {report['warning_count']} warnings")
        for item in report["issues"]:
            print(f"{item['severity'].upper()} [{item['code']}] {item['file']}: {item['detail']}")
        print("Windows 7 runtime validation is still required.")
        if report["unverified_system_dlls"]:
            print("System exports not verified: " + ", ".join(report["unverified_system_dlls"]))
        return 1 if report["error_count"] else 0
    except (AuditInputError, OSError, ValueError) as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
