"""Meaningful PE fixtures for the offline Windows 7 package audit.

Run: python -m unittest discover -s tests -p 'test_windows7_pe_audit.py'
Requires the same pinned pefile version as the build workflow.
"""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "windows7" / "audit_pe.py"
SPEC = importlib.util.spec_from_file_location("windows7_pe_audit", SCRIPT)
audit_pe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit_pe
SPEC.loader.exec_module(audit_pe)


def make_pe(path, imports=(), delay_imports=(), exports=(), machine=0x8664,
            subsystem=(6, 1), file_version=None):
    """Create a minimal parsed PE, including real import/export/resource tables.

    imports/delay_imports: [(dll, [name_or_ordinal, ...]), ...]
    exports: [(ordinal, name_or_None, forwarder_or_None), ...]
    The image is for parser tests only; no fixture is executed.
    """
    is_64 = machine == 0x8664
    pointer_format = "<Q" if is_64 else "<I"
    ordinal_mask = 1 << (63 if is_64 else 31)
    data = bytearray(b"\xC3" + b"\x00" * 15)
    directories = {}

    def allocate(value, alignment=4):
        data.extend(b"\x00" * (-len(data) % alignment))
        offset = len(data)
        data.extend(value)
        return offset + 0x1000

    def set_at(rva, fmt, *values):
        struct.pack_into(fmt, data, rva - 0x1000, *values)

    def string(value):
        return allocate(value.encode("ascii") + b"\x00", 1)

    for index, rows in ((1, imports), (13, delay_imports)):
        if not rows:
            continue
        descriptor_size = 20 if index == 1 else 32
        table = allocate(b"\x00" * (descriptor_size * (len(rows) + 1)))
        directories[index] = (table, descriptor_size * (len(rows) + 1))
        for row, (dll, symbols) in enumerate(rows):
            name = string(dll)
            thunks = []
            for symbol in symbols:
                if isinstance(symbol, str):
                    thunks.append(allocate(b"\x00\x00" + symbol.encode("ascii") + b"\x00", 2))
                else:
                    thunks.append(ordinal_mask | symbol)
            values = b"".join(struct.pack(pointer_format, value) for value in thunks + [0])
            lookup = allocate(values, 8)
            addresses = allocate(values, 8)
            if index == 1:
                set_at(table + row * descriptor_size, "<5I", lookup, 0, 0, name, addresses)
            else:
                set_at(table + row * descriptor_size, "<8I", 1, name, 0, addresses, lookup, 0, 0, 0)

    if exports:
        start = allocate(b"\x00" * 40)
        base = min(row[0] for row in exports)
        count = max(row[0] for row in exports) - base + 1
        functions = allocate(b"\x00" * (count * 4))
        named = sorted((row for row in exports if row[1] is not None), key=lambda row: row[1])
        names = allocate(b"\x00" * (len(named) * 4))
        ordinals = allocate(b"\x00" * (len(named) * 2), 2)
        dll = string(path.name)
        for ordinal, _name, forwarder in exports:
            address = string(forwarder) if forwarder else 0x1000
            set_at(functions + (ordinal - base) * 4, "<I", address)
        for i, (ordinal, name, _forwarder) in enumerate(named):
            set_at(names + i * 4, "<I", string(name))
            set_at(ordinals + i * 2, "<H", ordinal - base)
        set_at(start, "<IIHHIIIIIII", 0, 0, 0, 0, dll, base, count, len(named),
               functions, names, ordinals)
        directories[0] = (start, 0x1000 + len(data) - start)

    if file_version is not None:
        major, minor, build, revision = file_version
        ms, ls = (major << 16) | minor, (build << 16) | revision
        fixed = struct.pack("<13I", 0xFEEF04BD, 0x10000, ms, ls, ms, ls, 0x3F, 0, 0x40004, 2, 0, 0, 0)
        key = "VS_VERSION_INFO\x00".encode("utf-16-le")
        value = bytearray(b"\x00" * 6 + key)
        value.extend(b"\x00" * (-len(value) % 4))
        value.extend(fixed)
        struct.pack_into("<3H", value, 0, len(value), len(fixed), 0)
        resource = allocate(b"\x00" * 88 + value)
        for offset in (0, 24, 48):
            set_at(resource + offset + 12, "<2H", 0, 1)
        set_at(resource + 16, "<2I", 16, 0x80000018)
        set_at(resource + 40, "<2I", 1, 0x80000030)
        set_at(resource + 64, "<2I", 1033, 72)
        set_at(resource + 72, "<4I", resource + 88, len(value), 1200, 0)
        directories[2] = (resource, len(value) + 88)

    headers = bytearray(0x200)
    headers[:2] = b"MZ"
    struct.pack_into("<I", headers, 0x3C, 0x80)
    headers[0x80:0x84] = b"PE\x00\x00"
    optional_size = 240 if is_64 else 224
    struct.pack_into("<HHIIIHH", headers, 0x84, machine, 1, 0, 0, 0, optional_size, 0x2022)
    optional = 0x98
    struct.pack_into("<H", headers, optional, 0x20B if is_64 else 0x10B)
    struct.pack_into("<I", headers, optional + 16, 0x1000)
    if is_64:
        struct.pack_into("<Q", headers, optional + 24, 0x180000000)
    else:
        struct.pack_into("<I", headers, optional + 28, 0x400000)
    struct.pack_into("<2I", headers, optional + 32, 0x1000, 0x200)
    struct.pack_into("<2H", headers, optional + 40, 6, 1)
    struct.pack_into("<2H", headers, optional + 48, *subsystem)
    struct.pack_into("<2I", headers, optional + 56, (0x1000 + len(data) + 0xFFF) & ~0xFFF, 0x200)
    struct.pack_into("<2H", headers, optional + 68, 3, 0x8160)
    data_offset = 112 if is_64 else 96
    struct.pack_into("<I", headers, optional + data_offset - 4, 16)
    for index, values in directories.items():
        struct.pack_into("<2I", headers, optional + data_offset + index * 8, *values)
    raw_size = (len(data) + 0x1FF) & ~0x1FF
    section = optional + optional_size
    struct.pack_into("<8s6I2HI", headers, section, b".rdata\x00\x00", len(data), 0x1000,
                     raw_size, 0x200, 0, 0, 0, 0, 0x40000040)
    data.extend(b"\x00" * (raw_size - len(data)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(headers + data)
    return path


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / "package"
        self.package.mkdir()

    def reference(self, exports=()):
        directory = self.root / "system32"
        make_pe(directory / "kernel32.dll", exports=exports, file_version=(6, 1, 7601, 24545))
        return directory

    def codes(self, report, severity="error"):
        return {issue["code"] for issue in report["issues"] if issue["severity"] == severity}

    def test_parses_real_pe_import_names_ordinals_and_delay_imports(self):
        path = make_pe(self.package / "app.exe",
                       imports=[("KERNEL32.dll", ["Sleep", 25])],
                       delay_imports=[("USER32.dll", ["MessageBoxW"])])
        image = audit_pe.read_image(path)
        self.assertEqual(image.machine, 0x8664)
        self.assertEqual(image.imports[0].symbols, ("Sleep", 25))
        self.assertEqual(image.imports[0].dll, "kernel32.dll")
        self.assertFalse(image.imports[0].delayed)
        self.assertTrue(image.imports[1].delayed)

    def test_preflight_does_not_claim_system_exports_were_checked(self):
        make_pe(self.package / "app.exe", imports=[("kernel32.dll", ["Sleep"])])
        report = audit_pe.audit(self.package)
        self.assertEqual(report["status"], "preflight-only")
        self.assertEqual(report["unverified_system_dlls"], ["kernel32.dll"])
        self.assertTrue(report["runtime_validation_required"])

    def test_rejects_modern_api_even_when_pe_version_says_windows7(self):
        make_pe(self.package / "app.exe", imports=[("kernel32.dll", ["WaitOnAddress"])])
        report = audit_pe.audit(self.package)
        self.assertIn("post-win7-api", self.codes(report))
        self.assertEqual(report["status"], "blocked")

    def test_delay_import_is_reported_separately_from_startup_blocker(self):
        make_pe(self.package / "app.exe", delay_imports=[("shcore.dll", ["GetDpiForMonitor"])])
        report = audit_pe.audit(self.package)
        self.assertEqual(report["error_count"], 0)
        self.assertIn("post-win7-dll", self.codes(report, "warning"))
        self.assertIn("post-win7-dll", self.codes(audit_pe.audit(self.package, strict_delay=True)))

    def test_winrt_api_sets_are_blocked_even_with_original_contract_versions(self):
        for suffix, symbol in (("l1-1-0", "RoGetActivationFactory"),
                               ("error-l1-1-0", "RoOriginateErrorW"),
                               ("string-l1-1-0", "WindowsCreateString")):
            with self.subTest(suffix=suffix):
                contract = f"api-ms-win-core-winrt-{suffix}.dll"
                make_pe(self.package / "app.exe", imports=[(contract, [symbol])])
                report = audit_pe.audit(self.package)
                self.assertIn("post-win7-dll", self.codes(report))
                self.assertEqual(report["status"], "blocked")

    def test_existing_win7_timer_and_cryptography_dll_are_not_false_blockers(self):
        make_pe(self.package / "app.exe", imports=[
            ("kernel32.dll", ["SetWaitableTimerEx"]),
            ("bcryptprimitives.dll", ["GetPrimitiveInterface"]),
        ])
        report = audit_pe.audit(self.package)
        self.assertEqual(report["error_count"], 0, report["issues"])
        self.assertEqual(report["status"], "preflight-only")
        self.assertIn("bcryptprimitives.dll", report["unverified_system_dlls"])

    def test_win7_sas_and_driver_imports_remain_unverified_in_preflight(self):
        make_pe(self.package / "librustdesk.dll", imports=[
            ("Sas.dll", ["SendSAS"]),
            ("Newdev.dll", ["UpdateDriverForPlugAndPlayDevicesW"]),
        ])
        report = audit_pe.audit(self.package)
        self.assertEqual(report["error_count"], 0, report["issues"])
        self.assertEqual(report["status"], "preflight-only")
        self.assertEqual(report["unverified_system_dlls"], ["newdev.dll", "sas.dll"])
        self.assertTrue(all(item["status"] == "unverified-system" for item in report["imports"]))

        make_pe(self.package / "plugin.dll", imports=[("missing-plugin-runtime.dll", ["Init"])])
        report = audit_pe.audit(self.package)
        self.assertEqual(report["error_count"], 1, report["issues"])
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(self.codes(report), {"missing-dll"})
        self.assertIn("missing-plugin-runtime.dll", report["issues"][0]["detail"])

    def test_win7_sas_and_driver_imports_require_reference_dlls_and_exports(self):
        imports = [
            ("sas.dll", ["SendSAS"]),
            ("newdev.dll", ["UpdateDriverForPlugAndPlayDevicesW"]),
        ]
        make_pe(self.package / "librustdesk.dll", imports=imports)
        system = self.reference()
        report = audit_pe.audit(self.package, system_dir=system)
        self.assertEqual(report["error_count"], 2, report["issues"])
        self.assertEqual(self.codes(report), {"unresolved-reference"})
        self.assertEqual(report["unverified_system_dlls"], [])

        for dll, _symbols in imports:
            make_pe(system / dll, exports=[(1, "OtherExport", None)])
        report = audit_pe.audit(self.package, system_dir=system)
        self.assertEqual(report["error_count"], 2, report["issues"])
        self.assertEqual(self.codes(report), {"missing-export"})
        self.assertEqual(report["unverified_system_dlls"], [])

        for dll, symbols in imports:
            make_pe(system / dll, exports=[(1, symbols[0], None)])
        report = audit_pe.audit(self.package, system_dir=system)
        self.assertEqual(report["error_count"], 0, report["issues"])
        self.assertEqual(report["status"], "reference-checked")
        self.assertEqual(report["unverified_system_dlls"], [])
        self.assertTrue(all(item["status"] == "resolved" for item in report["imports"]))

    def test_processprng_is_rejected_as_missing_api_not_missing_dll(self):
        make_pe(self.package / "app.exe", imports=[("bcryptprimitives.dll", ["ProcessPrng"])])
        self.assertIn("post-win7-api", self.codes(audit_pe.audit(self.package)))

    def test_checks_nested_dlls_and_missing_dependencies(self):
        make_pe(self.package / "app.exe")
        make_pe(self.package / "plugins" / "plugin.dll", imports=[("missing-plugin-runtime.dll", ["Init"])])
        report = audit_pe.audit(self.package)
        self.assertEqual(len(report["files"]), 2)
        self.assertIn("missing-dll", self.codes(report))

    def test_rejects_missing_export_in_bundled_dll(self):
        make_pe(self.package / "app.exe", imports=[("plugin.dll", ["Missing"])])
        make_pe(self.package / "plugin.dll", exports=[(1, "Present", None)])
        self.assertIn("missing-export", self.codes(audit_pe.audit(self.package)))

    def test_architecture_subsystem_and_bad_pe_are_errors(self):
        make_pe(self.package / "wrong-arch.dll", machine=0x14C)
        make_pe(self.package / "modern.exe", subsystem=(10, 0))
        (self.package / "broken.dll").write_bytes(b"not a PE")
        codes = self.codes(audit_pe.audit(self.package))
        self.assertTrue({"architecture", "subsystem-version", "invalid-pe"} <= codes)

    def test_reference_checks_named_and_ordinal_exports_and_forwarders(self):
        system = self.reference(exports=[(1, "Sleep", "NTDLL.NtDelayExecution")])
        make_pe(system / "ntdll.dll", exports=[(12, "NtDelayExecution", None)])
        make_pe(self.package / "helper.dll", exports=[(3, None, "KERNEL32.Sleep")])
        make_pe(self.package / "app.exe", imports=[("helper.dll", [3]), ("kernel32.dll", ["Sleep"])])
        report = audit_pe.audit(self.package, system_dir=system)
        self.assertEqual(report["error_count"], 0, report["issues"])
        self.assertEqual(report["status"], "reference-checked")
        self.assertEqual(report["unverified_system_dlls"], [])
        self.assertTrue(report["runtime_validation_required"])

    def test_reference_missing_export_is_not_hidden_by_common_dll_list(self):
        system = self.reference(exports=[(1, "Sleep", None)])
        make_pe(self.package / "app.exe", imports=[("kernel32.dll", ["AbsentExport"])])
        self.assertIn("missing-export", self.codes(audit_pe.audit(self.package, system_dir=system)))

    def test_forwarder_cycles_and_missing_forwarder_targets_fail(self):
        make_pe(self.package / "app.exe", imports=[("a.dll", ["Start", "Missing"])])
        make_pe(self.package / "a.dll", exports=[(1, "Start", "b.Start"), (2, "Missing", "missing.Entry")])
        make_pe(self.package / "b.dll", exports=[(1, "Start", "a.Start")])
        codes = self.codes(audit_pe.audit(self.package))
        self.assertTrue({"forwarder-cycle", "missing-dll"} <= codes)

    def test_reference_rejects_windows10_and_wrong_architecture(self):
        make_pe(self.package / "app.exe")
        system = self.reference()
        make_pe(system / "kernel32.dll", file_version=(10, 0, 19045, 0))
        with self.assertRaisesRegex(audit_pe.AuditInputError, "6.1.7601"):
            audit_pe.audit(self.package, system_dir=system)
        make_pe(system / "kernel32.dll", machine=0x14C, file_version=(6, 1, 7601, 0))
        with self.assertRaisesRegex(audit_pe.AuditInputError, "architecture"):
            audit_pe.audit(self.package, system_dir=system)

    def test_api_set_mapping_requires_matching_real_export(self):
        system = self.reference(exports=[(1, "Sleep", None)])
        contract = "api-ms-win-core-test-l1-1-0.dll"
        make_pe(self.package / "app.exe", imports=[(contract, ["Sleep"])])
        before = audit_pe.audit(self.package, system_dir=system)
        self.assertIn("unresolved-reference", self.codes(before))
        after = audit_pe.audit(self.package, system_dir=system, api_sets={contract: "kernel32.dll"})
        self.assertEqual(after["error_count"], 0, after["issues"])

    def test_bundled_known_dll_cannot_fake_availability_of_a_new_api(self):
        make_pe(self.package / "app.exe", imports=[("kernel32.dll", ["WaitOnAddress"])])
        make_pe(self.package / "kernel32.dll", exports=[(1, "WaitOnAddress", None)])
        codes = self.codes(audit_pe.audit(self.package))
        self.assertTrue({"bundled-known-dll", "post-win7-api"} <= codes)

    def test_cli_report_and_required_reference_exit_codes(self):
        make_pe(self.package / "app.exe")
        output = self.root / "reports" / "audit.json"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(audit_pe.main([str(self.package), "--json", str(output)]), 0)
            self.assertEqual(audit_pe.main([str(self.package), "--strict-reference"]), 2)
        self.assertEqual(json.loads(output.read_text())["status"], "preflight-only")
        make_pe(self.package / "bad.exe", imports=[("kernel32.dll", ["WaitOnAddress"])])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(audit_pe.main([str(self.package)]), 1)


if __name__ == "__main__":
    unittest.main()
