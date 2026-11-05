"""Exercise the real CMake prebuilt-DLL handoff without a Windows compiler.

Only Flutter's generated CMake targets are stubbed. This checks configuration
and install paths, not compilation, Windows loading, or Windows 7 support.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("cmake"), "CMake is required")
class PrebuiltBridgeConfiguration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rustdesk win7 cmake ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.windows = self.root / "windows"
        self.windows.mkdir()
        shutil.copy2(REPO / "flutter/windows/CMakeLists.txt", self.windows)
        shutil.copytree(REPO / "flutter/windows/runner", self.windows / "runner")
        managed = self.windows / "flutter"
        managed.mkdir()
        (managed / "generated_plugin_registrant.cc").write_text("", encoding="utf-8")
        (managed / "generated_plugins.cmake").write_text("", encoding="utf-8")
        for name in ("stub.dll", "icudtl.dat", "app.so"):
            (managed / name).write_bytes(b"fixture")
        (managed / "CMakeLists.txt").write_text(
            "add_library(flutter INTERFACE)\n"
            "add_library(flutter_wrapper_app INTERFACE)\n"
            "add_custom_target(flutter_assemble)\n"
            'set(FLUTTER_LIBRARY "${CMAKE_CURRENT_SOURCE_DIR}/stub.dll" PARENT_SCOPE)\n'
            'set(FLUTTER_ICU_DATA_FILE "${CMAKE_CURRENT_SOURCE_DIR}/icudtl.dat" PARENT_SCOPE)\n'
            'set(AOT_LIBRARY "${CMAKE_CURRENT_SOURCE_DIR}/app.so" PARENT_SCOPE)\n'
            'set(PROJECT_BUILD_DIR "${CMAKE_CURRENT_BINARY_DIR}" PARENT_SCOPE)\n',
            encoding="utf-8",
        )
        # Query actual target creation rather than matching the implementation.
        with (self.windows / "CMakeLists.txt").open("a", encoding="utf-8") as stream:
            stream.write(
                '\nif(TARGET build_rustdesk_lib)\n'
                '  file(WRITE "${CMAKE_BINARY_DIR}/rust-target.txt" "present")\n'
                'else()\n'
                '  file(WRITE "${CMAKE_BINARY_DIR}/rust-target.txt" "absent")\n'
                'endif()\n'
            )

    def configure(self, bridge=None, virtual_display=None):
        env = os.environ.copy()
        env.pop("RUSTDESK_PREBUILT_LIB", None)
        env.pop("RUSTDESK_PREBUILT_VIRTUAL_DISPLAY_LIB", None)
        if bridge is not None:
            env["RUSTDESK_PREBUILT_LIB"] = str(bridge)
        if virtual_display is not None:
            env["RUSTDESK_PREBUILT_VIRTUAL_DISPLAY_LIB"] = str(virtual_display)
        self.build = self.root / "configured"
        return subprocess.run(
            ["cmake", "-S", str(self.windows), "-B", str(self.build)],
            env=env, text=True, capture_output=True, check=False,
        )

    def test_default_build_keeps_rust_target(self):
        result = self.configure()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.build / "rust-target.txt").read_text(), "present")

    def test_prebuilt_path_with_spaces_skips_rust_and_installs_exact_dll(self):
        directory = self.root / "prebuilt libraries"
        directory.mkdir()
        bridge = directory / "librustdesk.dll"
        bridge.write_bytes(b"fixture")
        result = self.configure(bridge)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.build / "rust-target.txt").read_text(), "absent")
        install = (self.build / "cmake_install.cmake").read_text()
        self.assertIn(bridge.as_posix(), install)
        self.assertNotIn("dylib_virtual_display.dll", install)

    def test_nonexistent_or_relative_prebuilt_path_is_rejected(self):
        for path in (self.root / "missing.dll", Path("relative.dll")):
            with self.subTest(path=path):
                if (self.root / "configured").exists():
                    shutil.rmtree(self.root / "configured")
                result = self.configure(path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("existing absolute DLL path", result.stderr)

    def test_optional_virtual_display_must_exist_when_explicitly_provided(self):
        bridge = self.root / "librustdesk.dll"
        bridge.write_bytes(b"fixture")
        result = self.configure(bridge, self.root / "missing-virtual-display.dll")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RUSTDESK_PREBUILT_VIRTUAL_DISPLAY_LIB", result.stderr)


if __name__ == "__main__":
    unittest.main()
