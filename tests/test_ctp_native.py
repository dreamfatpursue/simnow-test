import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import live_grid.ctp_native as ctp_native
from live_grid.ctp_native import (
    CTP_NATIVE_VARIANT_BY_ENV,
    activate_ctp_native_libs,
    variant_for_environment,
)


def _write(path: Path, data: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _fake_project(root: Path) -> tuple[Path, SimpleNamespace, dict[str, Path]]:
    vendor = root / "vendor"
    package = vendor / "vnpy_ctp"
    api = package / "api"
    _write(package / "__init__.py")
    _write(api / "__init__.py")
    _write(api / "ctp_constant.py")

    build = vendor / "build" / f"cp{sys.version_info.major}{sys.version_info.minor}"
    for name in ("vnctptd", "vnctpmd"):
        _write(build / f"{name}.cpython-test.so", name.encode())

    slots: dict[str, Path] = {}
    for name in ctp_native._NATIVE_NAMES:
        framework = api / f"{name}.framework"
        slots[name] = framework / "Versions/A" / name
        _write(slots[name], f"tracked-{name}".encode())
        for variant in ("simnow", "guangfa"):
            _write(
                api / "ctp_variants" / variant / name,
                f"{variant}-{name}".encode(),
            )

    fake_package = SimpleNamespace(
        __file__=str(package / "__init__.py"),
        __path__=[str(package)],
    )
    return api, fake_package, slots


class CtpNativeVariantTests(unittest.TestCase):
    def setUp(self) -> None:
        if ctp_native._RUNTIME_DIRECTORY is not None:
            ctp_native._RUNTIME_DIRECTORY.cleanup()
        ctp_native._RUNTIME_DIRECTORY = None
        ctp_native._RUNTIME_VARIANT = None
        ctp_native._RUNTIME_FAILURE = None

    def tearDown(self) -> None:
        if ctp_native._RUNTIME_DIRECTORY is not None:
            ctp_native._RUNTIME_DIRECTORY.cleanup()
        ctp_native._RUNTIME_DIRECTORY = None
        ctp_native._RUNTIME_VARIANT = None
        ctp_native._RUNTIME_FAILURE = None

    def _activate(
        self,
        environment: str,
        api: Path,
        package: SimpleNamespace,
        temp_root: str,
        load_error: Exception | None = None,
    ) -> str:
        extension_loader = (
            patch.object(ctp_native, "_load_darwin_extension", side_effect=load_error)
            if load_error is not None
            else patch.object(ctp_native, "_load_darwin_extension", return_value=None)
        )
        with (
            patch.object(ctp_native.platform, "system", return_value="Darwin"),
            patch.object(ctp_native.importlib, "import_module", return_value=package),
            patch.object(ctp_native, "_ctp_api_loaded", return_value=False),
            patch.object(ctp_native.tempfile, "tempdir", temp_root),
            extension_loader as load_extension,
            patch.dict(sys.modules, {"vnpy_ctp": package}, clear=False),
        ):
            sys.modules.pop("vnpy_ctp.api", None)
            result = activate_ctp_native_libs(environment, api_dir=api)
            self.extension_load_calls = load_extension.call_args_list
            return result

    def test_environment_maps_to_variant(self) -> None:
        self.assertEqual(variant_for_environment("first"), "simnow")
        self.assertEqual(variant_for_environment("7x24"), "simnow")
        self.assertEqual(variant_for_environment("guangfa"), "guangfa")
        self.assertEqual(CTP_NATIVE_VARIANT_BY_ENV["7x24"], "simnow")

    def test_activate_uses_isolated_runtime_and_preserves_tracked_frameworks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for environment, variant in (("7x24", "simnow"), ("guangfa", "guangfa")):
                if ctp_native._RUNTIME_DIRECTORY is not None:
                    ctp_native._RUNTIME_DIRECTORY.cleanup()
                    ctp_native._RUNTIME_DIRECTORY = None
                    ctp_native._RUNTIME_VARIANT = None
                    ctp_native._RUNTIME_FAILURE = None
                api, package, slots = _fake_project(Path(tmp) / variant)
                self.assertEqual(self._activate(environment, api, package, tmp), variant)
                runtime_package = Path(ctp_native._RUNTIME_DIRECTORY.name) / "vnpy_ctp"
                runtime_api = runtime_package / "api"
                for name in ctp_native._NATIVE_NAMES:
                    self.assertEqual(
                        (runtime_api / f"{name}.framework/Versions/A" / name).read_bytes(),
                        f"{variant}-{name}".encode(),
                    )
                    self.assertEqual(slots[name].read_bytes(), f"tracked-{name}".encode())
                for name in ctp_native._API_EXTENSION_NAMES:
                    self.assertTrue(list(runtime_api.glob(f"{name}*")))
                self.assertEqual(
                    [call.args[0] for call in self.extension_load_calls],
                    [f"vnpy_ctp.api.{name}" for name in ctp_native._API_EXTENSION_NAMES],
                )
                self.assertTrue(
                    all(call.args[1].parent == runtime_api for call in self.extension_load_calls)
                )
                api_module = package.__dict__["api"]
                self.assertEqual(api_module.__dict__["__file__"], str(runtime_api / "__init__.py"))
                self.assertEqual(
                    api_module.__dict__["__spec__"].submodule_search_locations,
                    [str(runtime_api)],
                )
                self.assertEqual(self._activate(environment, api, package, tmp), variant)

    def test_activate_rejects_switch_after_ctp_api_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api, package, _ = _fake_project(Path(tmp))
            self._activate("first", api, package, tmp)
            with patch.object(ctp_native.platform, "system", return_value="Darwin"):
                with self.assertRaisesRegex(RuntimeError, "当前进程已选择"):
                    activate_ctp_native_libs("guangfa", api_dir=api)

    def test_failed_native_load_requires_a_new_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api, package, _ = _fake_project(Path(tmp))
            with self.assertRaisesRegex(ImportError, "native load failed"):
                self._activate("first", api, package, tmp, ImportError("native load failed"))
            with patch.object(ctp_native.platform, "system", return_value="Darwin"):
                with self.assertRaisesRegex(RuntimeError, "必须用新进程重启"):
                    activate_ctp_native_libs("guangfa", api_dir=api)


if __name__ == "__main__":
    unittest.main()
