"""Select SimNow vs 广发 CTP native libraries before the gateway loads.

Today's 广发适配把 active framework 换成了看穿式 `v6.7.7_MacOS_CP`，
该版本能连广发仿真，但会对 SimNow / 7×24 前置报 4040 handshake decode err。
标准版 `v6.7.7_MacOS` 则相反。两套原生库并存，按 `--env` 在进程加载扩展前切换。
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.machinery
import importlib.util
import platform
import shutil
import sys
import tempfile
from pathlib import Path


CTP_NATIVE_VARIANT_BY_ENV = {
    "first": "simnow",
    "7x24": "simnow",
    "guangfa": "guangfa",
}

_NATIVE_NAMES = ("thosttraderapi_se", "thostmduserapi_se")
_API_EXTENSION_NAMES = ("vnctptd", "vnctpmd")
_RUNTIME_DIRECTORY: tempfile.TemporaryDirectory | None = None
_RUNTIME_VARIANT: str | None = None
_RUNTIME_FAILURE: str | None = None


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


def _ctp_api_loaded() -> bool:
    return any(name == "vnpy_ctp.api" or name.startswith("vnpy_ctp.api.") for name in sys.modules)


def _load_darwin_extension(fullname: str, path: Path) -> object:
    spec = importlib.util.spec_from_file_location(fullname, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法创建隔离的 CTP 扩展加载入口: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(fullname, None)
        raise
    return module


def _activate_darwin_isolated(api: Path, variant: str) -> str:
    global _RUNTIME_DIRECTORY, _RUNTIME_FAILURE, _RUNTIME_VARIANT

    if _RUNTIME_FAILURE is not None:
        raise RuntimeError(f"CTP 原生扩展初始化失败，必须用新进程重启: {_RUNTIME_FAILURE}")
    if _RUNTIME_DIRECTORY is not None:
        if _RUNTIME_VARIANT == variant:
            return variant
        raise RuntimeError("当前进程已选择 CTP 原生库变体，必须用匹配环境的新进程启动")
    if _ctp_api_loaded():
        raise RuntimeError("CTP 原生扩展已加载，必须在导入 vnpy_ctp.api 前选择环境")

    package = importlib.import_module("vnpy_ctp")
    source_package = api.parent.resolve()
    if Path(package.__file__).resolve() != source_package / "__init__.py":
        raise RuntimeError(f"CTP 包未加载项目内源码: {package.__file__}")

    runtime = tempfile.TemporaryDirectory(prefix="simnow-ctp-")
    api_module = None
    extension_load_started = False
    try:
        runtime_package = Path(runtime.name) / "vnpy_ctp"
        runtime_api = runtime_package / "api"
        runtime_api.mkdir(parents=True)
        for name in ("__init__.py", "ctp_constant.py"):
            shutil.copy2(api / name, runtime_api / name)

        build_dir = (
            api.parents[1]
            / "build"
            / f"cp{sys.version_info.major}{sys.version_info.minor}"
        )
        runtime_extensions: dict[str, Path] = {}
        for name in _API_EXTENSION_NAMES:
            extensions = [path for path in build_dir.glob(f"{name}*") if path.is_file()]
            if not extensions:
                raise FileNotFoundError(f"缺少 CTP 原生扩展: {build_dir}/{name}*")
            for extension in extensions:
                shutil.copy2(extension, runtime_api / extension.name)
            runtime_extension = next(
                (
                    runtime_api / extension.name
                    for extension in extensions
                    if any(
                        extension.name.endswith(suffix)
                        for suffix in importlib.machinery.EXTENSION_SUFFIXES
                    )
                ),
                None,
            )
            if runtime_extension is None:
                raise FileNotFoundError(f"缺少可加载的 CTP 原生扩展: {build_dir}/{name}*")
            runtime_extensions[name] = runtime_extension

        source_variant = api / "ctp_variants" / variant
        runtime_slots = _darwin_active_slots(runtime_api)
        for name in _NATIVE_NAMES:
            source = source_variant / name
            if not source.is_file():
                raise FileNotFoundError(f"缺少 CTP 原生库文件: {source}")
            framework = api / f"{name}.framework"
            if not framework.is_dir():
                raise FileNotFoundError(f"缺少 CTP framework: {framework}")
            shutil.copytree(framework, runtime_api / framework.name, symlinks=True)
            shutil.copy2(source, runtime_slots[name])

        spec = importlib.util.spec_from_file_location(
            "vnpy_ctp.api",
            runtime_api / "__init__.py",
            submodule_search_locations=[str(runtime_api)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"无法创建隔离的 CTP API 加载入口: {runtime_api}")
        api_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = api_module
        package.api = api_module

        for name, extension in runtime_extensions.items():
            fullname = f"vnpy_ctp.api.{name}"
            extension_load_started = True
            extension_module = _load_darwin_extension(fullname, extension)
            setattr(api_module, name, extension_module)
        spec.loader.exec_module(api_module)
    except Exception as exc:
        if api_module is not None and sys.modules.get("vnpy_ctp.api") is api_module:
            del sys.modules["vnpy_ctp.api"]
        if api_module is not None and vars(package).get("api") is api_module:
            delattr(package, "api")
        if extension_load_started:
            _RUNTIME_DIRECTORY = runtime
            _RUNTIME_VARIANT = variant
            _RUNTIME_FAILURE = str(exc)
        else:
            runtime.cleanup()
        raise

    _RUNTIME_DIRECTORY = runtime
    _RUNTIME_VARIANT = variant
    return variant


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
    if platform.system() == "Darwin":
        return _activate_darwin_isolated(api, variant)

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

    if replacements and _ctp_api_loaded():
        raise RuntimeError(
            f"CTP 原生扩展已加载，无法再切换到变体 {variant}；请用匹配环境的新进程启动"
        )

    for src, dest in replacements:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".activating")
        shutil.copy2(src, tmp)
        tmp.replace(dest)

    return variant
