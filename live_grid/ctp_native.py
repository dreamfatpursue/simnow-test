"""Select SimNow vs 广发 CTP native libraries before the gateway loads.

Today's 广发适配把 active framework 换成了看穿式 `v6.7.7_MacOS_CP`，
该版本能连广发仿真，但会对 SimNow / 7×24 前置报 4040 handshake decode err。
标准版 `v6.7.7_MacOS` 则相反。两套原生库并存，按 `--env` 在进程加载扩展前切换。
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import sys
from pathlib import Path


CTP_NATIVE_VARIANT_BY_ENV = {
    "first": "simnow",
    "7x24": "simnow",
    "guangfa": "guangfa",
}

_NATIVE_NAMES = ("thosttraderapi_se", "thostmduserapi_se")
_LOADED_MODULE_NAMES = ("vnpy_ctp.api.vnctptd", "vnpy_ctp.api.vnctpmd")


def variant_for_environment(environment: str) -> str:
    try:
        return CTP_NATIVE_VARIANT_BY_ENV[environment]
    except KeyError as exc:
        raise ValueError(f"未知连接环境: {environment}") from exc


def default_api_dir(project_root: Path | None = None) -> Path:
    root = Path(project_root or Path(__file__).resolve().parents[1])
    return root / "vendor" / "vnpy_ctp" / "vnpy_ctp" / "api"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _darwin_active_slots(api_dir: Path) -> dict[str, Path]:
    return {
        "thosttraderapi_se": api_dir / "thosttraderapi_se.framework" / "Versions" / "A" / "thosttraderapi_se",
        "thostmduserapi_se": api_dir / "thostmduserapi_se.framework" / "Versions" / "A" / "thostmduserapi_se",
    }


def _posix_or_windows_slots(api_dir: Path) -> dict[str, Path]:
    system = platform.system()
    if system == "Windows":
        return {
            "thosttraderapi_se": api_dir / "thosttraderapi_se.dll",
            "thostmduserapi_se": api_dir / "thostmduserapi_se.dll",
        }
    return {
        "thosttraderapi_se": api_dir / "libthosttraderapi_se.so",
        "thostmduserapi_se": api_dir / "libthostmduserapi_se.so",
    }


def _active_slots(api_dir: Path) -> dict[str, Path]:
    if platform.system() == "Darwin":
        return _darwin_active_slots(api_dir)
    return _posix_or_windows_slots(api_dir)


def _native_modules_loaded() -> bool:
    return any(name in sys.modules for name in _LOADED_MODULE_NAMES)


def activate_ctp_native_libs(
    environment: str,
    *,
    api_dir: Path | None = None,
    project_root: Path | None = None,
) -> str:
    """Install the env-matched CTP native libs into the active load slots.

    Must run before `vnpy_ctp.api.vnctptd` / `vnctpmd` are imported. Switching
    after load is impossible in-process because dyld keeps the first image.
    """
    variant = variant_for_environment(environment)
    api = Path(api_dir) if api_dir is not None else default_api_dir(project_root)
    src_dir = api / "ctp_variants" / variant
    if not src_dir.is_dir():
        raise FileNotFoundError(f"缺少 CTP 原生库变体目录: {src_dir}")

    slots = _active_slots(api)
    replacements: list[tuple[Path, Path]] = []
    for name in _NATIVE_NAMES:
        src = src_dir / name
        dest = slots[name]
        if not src.is_file():
            raise FileNotFoundError(f"缺少 CTP 原生库文件: {src}")
        if dest.is_file() and _sha256(src) == _sha256(dest):
            continue
        replacements.append((src, dest))

    if replacements and _native_modules_loaded():
        raise RuntimeError(
            f"CTP 原生扩展已加载，无法再切换到变体 {variant}；请用匹配环境的新进程启动"
        )

    for src, dest in replacements:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".activating")
        shutil.copy2(src, tmp)
        tmp.replace(dest)

    return variant
