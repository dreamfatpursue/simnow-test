import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live_grid.ctp_native import (
    CTP_NATIVE_VARIANT_BY_ENV,
    activate_ctp_native_libs,
    variant_for_environment,
)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CtpNativeVariantTests(unittest.TestCase):
    def test_environment_maps_to_variant(self) -> None:
        self.assertEqual(variant_for_environment("first"), "simnow")
        self.assertEqual(variant_for_environment("7x24"), "simnow")
        self.assertEqual(variant_for_environment("guangfa"), "guangfa")
        self.assertEqual(CTP_NATIVE_VARIANT_BY_ENV["7x24"], "simnow")

    def test_activate_copies_selected_variant_into_active_framework_slots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api = Path(tmp)
            for variant, trader, md in (
                ("simnow", b"simnow-trader", b"simnow-md"),
                ("guangfa", b"guangfa-trader", b"guangfa-md"),
            ):
                _write(api / "ctp_variants" / variant / "thosttraderapi_se", trader)
                _write(api / "ctp_variants" / variant / "thostmduserapi_se", md)
            trader_slot = api / "thosttraderapi_se.framework/Versions/A/thosttraderapi_se"
            md_slot = api / "thostmduserapi_se.framework/Versions/A/thostmduserapi_se"
            _write(trader_slot, b"stale-trader")
            _write(md_slot, b"stale-md")

            with patch("live_grid.ctp_native.platform.system", return_value="Darwin"):
                chosen = activate_ctp_native_libs("7x24", api_dir=api)

            self.assertEqual(chosen, "simnow")
            self.assertEqual(trader_slot.read_bytes(), b"simnow-trader")
            self.assertEqual(md_slot.read_bytes(), b"simnow-md")

            with patch("live_grid.ctp_native.platform.system", return_value="Darwin"):
                chosen = activate_ctp_native_libs("guangfa", api_dir=api)

            self.assertEqual(chosen, "guangfa")
            self.assertEqual(trader_slot.read_bytes(), b"guangfa-trader")
            self.assertEqual(md_slot.read_bytes(), b"guangfa-md")

    def test_activate_is_noop_when_active_already_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api = Path(tmp)
            payload = b"same-bytes"
            _write(api / "ctp_variants/simnow/thosttraderapi_se", payload)
            _write(api / "ctp_variants/simnow/thostmduserapi_se", payload)
            trader_slot = api / "thosttraderapi_se.framework/Versions/A/thosttraderapi_se"
            md_slot = api / "thostmduserapi_se.framework/Versions/A/thostmduserapi_se"
            _write(trader_slot, payload)
            _write(md_slot, payload)
            before_trader = trader_slot.stat().st_mtime_ns
            before_md = md_slot.stat().st_mtime_ns

            with patch("live_grid.ctp_native.platform.system", return_value="Darwin"):
                activate_ctp_native_libs("first", api_dir=api)

            self.assertEqual(trader_slot.stat().st_mtime_ns, before_trader)
            self.assertEqual(md_slot.stat().st_mtime_ns, before_md)
            self.assertEqual(_sha(trader_slot), _sha(api / "ctp_variants/simnow/thosttraderapi_se"))

    def test_activate_rejects_switch_after_native_modules_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api = Path(tmp)
            _write(api / "ctp_variants/simnow/thosttraderapi_se", b"simnow")
            _write(api / "ctp_variants/simnow/thostmduserapi_se", b"simnow")
            _write(api / "ctp_variants/guangfa/thosttraderapi_se", b"guangfa")
            _write(api / "ctp_variants/guangfa/thostmduserapi_se", b"guangfa")
            _write(api / "thosttraderapi_se.framework/Versions/A/thosttraderapi_se", b"guangfa")
            _write(api / "thostmduserapi_se.framework/Versions/A/thostmduserapi_se", b"guangfa")

            fake_modules = {"vnpy_ctp.api.vnctptd": object()}
            with (
                patch("live_grid.ctp_native.platform.system", return_value="Darwin"),
                patch.dict(sys.modules, fake_modules, clear=False),
            ):
                with self.assertRaisesRegex(RuntimeError, "已加载"):
                    activate_ctp_native_libs("7x24", api_dir=api)


if __name__ == "__main__":
    unittest.main()
