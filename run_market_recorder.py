#!/usr/bin/env python3
"""MD-only recorder with a private native runtime and a bounded supervisor."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import math
import os
import shlex
import shutil
import signal
import subprocess
import sys
import sysconfig
import threading
import time
from collections import deque
from pathlib import Path

from market_data_recorder import TickRecorder, append_json, atomic_json, load_config, new_run_id, recover_pending
from run import SETTING_ENV_BY_PROFILE

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / '.recorder-runtime'
VARIANTS = {'first': 'simnow', '7x24': 'simnow', 'guangfa': 'guangfa'}


def sha(path: Path) -> str:
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def runtime_paths(environment: str) -> tuple[Path, Path, Path]:
    if sys.platform != 'darwin':
        raise ValueError('当前独立原生运行副本仅验收 macOS；其他平台需先适配加载路径')
    suffix = sysconfig.get_config_var('EXT_SUFFIX')
    binding = ROOT / f'vendor/vnpy_ctp/build/cp{sys.version_info.major}{sys.version_info.minor}' / f'vnctpmd{suffix}'
    library = ROOT / 'vendor/vnpy_ctp/vnpy_ctp/api/ctp_variants' / VARIANTS[environment] / 'thostmduserapi_se'
    digest = hashlib.sha256(('md-runtime-v2' + sha(binding) + sha(library)).encode()).hexdigest()[:20]
    return PRIVATE / 'native' / digest, binding, library


def prepare_runtime(environment: str) -> Path:
    target, binding, library = runtime_paths(environment)
    if target.exists():
        check_runtime(environment)
        return target
    PRIVATE.mkdir(mode=0o700, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + '-' + new_run_id() + '.tmp')
    tmp.mkdir(mode=0o700)
    try:
        copied = tmp / binding.name
        shutil.copy2(binding, copied)
        shutil.copy2(library, tmp / library.name)
        subprocess.run(['codesign', '--force', '--sign', '-', str(tmp / library.name)],
                       check=True, capture_output=True)
        subprocess.run(['install_name_tool', '-change',
                        '@rpath/thostmduserapi_se.framework/Versions/A/thostmduserapi_se',
                        '@loader_path/thostmduserapi_se', str(copied)], check=True, capture_output=True)
        subprocess.run(['codesign', '--force', '--sign', '-', str(copied)], check=True, capture_output=True)
        atomic_json(tmp / 'runtime.json', dict(
            source_binding_sha256=sha(binding), source_library_sha256=sha(library),
            binding_name=binding.name, binding_sha256=sha(copied), library_sha256=sha(tmp / library.name)))
        os.rename(tmp, target)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    return target


def check_runtime(environment: str) -> tuple[Path, dict]:
    target, binding, library = runtime_paths(environment)
    if not target.is_dir():
        raise ValueError('采集原生副本尚未准备，请先执行 --prepare')
    info = json.loads((target / 'runtime.json').read_text())
    if info['binding_name'] != binding.name or info['source_binding_sha256'] != sha(binding) or info['source_library_sha256'] != sha(library):
        raise ValueError('采集原生副本来源校验失败')
    if info['binding_sha256'] != sha(target / binding.name) or info['library_sha256'] != sha(target / library.name):
        raise ValueError('采集原生副本文件校验失败')
    return target, info


def load_md_api(environment: str):
    target, info = check_runtime(environment)
    if 'vnctpmd' in sys.modules or any(n.startswith('vnpy_ctp.api') for n in sys.modules):
        raise ValueError('必须在未加载其他 CTP 扩展的独立进程中启动')
    spec = importlib.util.spec_from_file_location('vnctpmd', target / info['binding_name'])
    module = importlib.util.module_from_spec(spec)
    sys.modules['vnctpmd'] = module
    spec.loader.exec_module(module)
    # Check what dyld actually loaded; checking the copied files alone is insufficient.
    dyld = ctypes.CDLL(None)
    dyld._dyld_image_count.restype = ctypes.c_uint32
    dyld._dyld_get_image_name.argtypes = [ctypes.c_uint32]
    dyld._dyld_get_image_name.restype = ctypes.c_char_p
    loaded = [Path(dyld._dyld_get_image_name(i).decode()).resolve()
              for i in range(dyld._dyld_image_count())]
    ctp_images = [p for p in loaded if 'thostmduserapi' in p.name or 'thosttraderapi' in p.name]
    if ctp_images != [(target / 'thostmduserapi_se').resolve()]:
        raise ValueError('实际加载的 CTP 原生库未隔离')
    return module.MdApi, info | dict(native_directory=str(target), loaded_md_library=str(ctp_images[0]))


def load_md_settings(environment: str, env_file: Path) -> dict:
    names = SETTING_ENV_BY_PROFILE[environment]
    selected = {names[k] for k in ('user_id', 'password', 'broker_id', 'market_front')}
    values = {}
    for number, line in enumerate(env_file.read_text(encoding='utf-8').splitlines(), 1):
        stripped = line.strip().removeprefix('export ')
        key = stripped.split('=', 1)[0].strip()
        if key not in selected:
            continue
        try:
            tokens = shlex.split(stripped, comments=True)
        except ValueError:
            raise ValueError(f'.env 第 {number} 行引号不完整') from None
        if len(tokens) != 1 or '=' not in tokens[0]:
            raise ValueError(f'.env 第 {number} 行需使用 KEY=value，含空格值需加引号')
        actual, value = tokens[0].split('=', 1)
        values[actual] = value
    missing = sorted(selected - {k for k, v in values.items() if v.strip()})
    if missing:
        raise ValueError('缺少环境变量: ' + ', '.join(missing))
    address = values[names['market_front']]
    if '://' not in address:
        address = 'tcp://' + address
    if not address.startswith(('tcp://', 'ssl://', 'socks://')) or '\n' in address:
        raise ValueError('行情地址格式不合法')
    return dict(UserID=values[names['user_id']], Password=values[names['password']],
                BrokerID=values[names['broker_id']], address=address)


class MarketCallbacks:
    """Only MD callbacks; testable using a fake MD base without native imports."""
    def __init__(self, recorder: TickRecorder, settings: dict):
        super().__init__()
        self.recorder, self.settings = recorder, settings
        self.state_lock = threading.Lock()
        self.connected = self.logged_in = False
        self.connection_id = self.request_id = self.disconnects = 0
        self.confirmed = set()
        self.tick_counts = {s: 0 for s in recorder.contracts}
        self.failure = None
        self.next_login = self.login_sent_at = 0.0
        self.subscription_sent = False
        self.events = deque(maxlen=128)
        self.events_overflow = 0

    def _event(self, kind: str, **fields) -> None:
        if len(self.events) == self.events.maxlen:
            self.events_overflow += 1
        self.events.append(dict(kind=kind, epoch_ns=time.time_ns(), connection_id=self.connection_id, **fields))

    def onFrontConnected(self) -> None:
        with self.state_lock:
            self.connected = True
            self.logged_in = self.subscription_sent = False
            self.confirmed.clear()
            self.connection_id += 1
            self.login_sent_at = 0
            self._event('connected')

    def onFrontDisconnected(self, reason: int) -> None:
        with self.state_lock:
            self.connected = self.logged_in = self.subscription_sent = False
            self.confirmed.clear()
            self.disconnects += 1
            self.next_login = time.monotonic() + min(30, 2 ** min(self.disconnects, 5))
            self._event('disconnected', reason=int(reason))

    def onRspUserLogin(self, data: dict, error: dict, reqid: int, last: bool) -> None:
        with self.state_lock:
            code = int(error.get('ErrorID', 0))
            if code:
                self.failure = f'md_login_error:{code}'
            else:
                self.logged_in = True
                self._event('logged_in')

    def onRspSubMarketData(self, data: dict, error: dict, reqid: int, last: bool) -> None:
        with self.state_lock:
            code, symbol = int(error.get('ErrorID', 0)), data.get('InstrumentID')
            if code or symbol not in self.recorder.contracts:
                self.failure = f'md_subscription_error:{code}'
            else:
                self.confirmed.add(symbol)
                self._event('subscribed', symbol=symbol)

    def onRspError(self, error: dict, reqid: int, last: bool) -> None:
        with self.state_lock:
            self.failure = f'md_error:{int(error.get("ErrorID", 0))}'

    def onRtnDepthMarketData(self, data: dict) -> None:
        try:
            self.recorder.on_tick(data, self.connection_id)
            symbol = data.get('InstrumentID')
            if symbol in self.tick_counts:
                with self.state_lock:
                    self.tick_counts[symbol] += 1
        except Exception:
            self.recorder.fail('md_callback_failed')

    def poll(self, allow_requests: bool = True) -> dict:
        now = time.monotonic()
        with self.state_lock:
            login = allow_requests and self.connected and not self.logged_in and now >= self.next_login and (
                not self.login_sent_at or now - self.login_sent_at >= 30)
            if login:
                self.login_sent_at = now
                self.request_id += 1
            subscribe = allow_requests and self.logged_in and not self.subscription_sent
            if subscribe:
                self.subscription_sent = True
            events = list(self.events)
            self.events.clear()
            result = dict(connected=self.connected, logged_in=self.logged_in,
                          connection_id=self.connection_id, disconnects=self.disconnects,
                          subscriptions_confirmed=sorted(self.confirmed), ticks_by_symbol=dict(self.tick_counts),
                          md_error=self.failure, connection_event_overflow=self.events_overflow)
        if login:
            code = self.reqUserLogin({k: self.settings[k] for k in ('UserID', 'Password', 'BrokerID')}, self.request_id)
            if code:
                self.recorder.fail(f'md_login_send_error:{code}')
        if subscribe:
            for symbol in self.recorder.contracts:
                code = self.subscribeMarketData(symbol)
                if code:
                    self.recorder.fail(f'md_subscription_send_error:{code}')
        for event in events:
            append_json(self.recorder.directory / 'gaps.jsonl', event)
        return result


def worker(args, config: dict, settings: dict) -> int:
    try:
        os.nice(10)
    except PermissionError:
        # Lower priority is best-effort; restricted runtimes may deny it.
        pass
    base, native = load_md_api(args.env)
    recorder = TickRecorder(args.output, args.env, config, args.run_id)
    class RecorderMdApi(MarketCallbacks, base):
        pass
    api = RecorderMdApi(recorder, settings)
    flow = PRIVATE / 'flow' / recorder.run_id
    flow.mkdir(parents=True, mode=0o700)
    version_file = ROOT / 'vendor/vnpy_ctp/vnpy_ctp/api/ctp_variants' / VARIANTS[args.env] / 'VERSION.txt'
    recorder.start(dict(native=native, ctp_variant_version=version_file.read_text().strip()))
    started = time.monotonic()
    backlog_since = None
    state = {}
    api_started = False
    try:
        api.createFtdcMdApi(str(flow) + os.sep)
        api.registerFront(settings['address'])
        api.init()
        api_started = True
        while time.monotonic() - started < args.duration:
            state = api.poll()
            if state['md_error']:
                recorder.fail(state['md_error'])
            recorder.check_resources()
            if time.monotonic() - recorder.heartbeat > config['writer_timeout_seconds']:
                recorder.fail('writer_heartbeat_timeout')
            if recorder.error:
                break
            if recorder.queue.qsize() >= config['queue_size'] * 0.8:
                backlog_since = backlog_since or time.monotonic()
            else:
                backlog_since = None
            if backlog_since and time.monotonic() - backlog_since > config['writer_timeout_seconds']:
                recorder.fail('persistent_queue_backlog')
                break
            if time.monotonic() - started > args.startup_timeout and (
                set(state['subscriptions_confirmed']) != set(recorder.contracts)
                or not all(state['ticks_by_symbol'].values())):
                recorder.fail('startup_timeout_missing_subscription_or_ticks')
                break
            healthy = state['logged_in'] and set(state['subscriptions_confirmed']) == set(recorder.contracts)
            recorder.write_status(state, 'RECORDING' if healthy else 'CONNECTING')
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        reason = str(exc) if isinstance(exc, RuntimeError) and str(exc) in {
            'disk_reserve_reached', 'memory_limit_reached'
        } else type(exc).__name__
        recorder.fail('management_failed:' + reason)
    finally:
        # Native exit can block; the supervisor enforces a total shutdown deadline.
        with recorder.lock:
            recorder.accepting = False
        if api_started:
            def close_md():
                try:
                    api.exit()
                except Exception:
                    recorder.fail('md_close_failed')
            closer = threading.Thread(target=close_md, daemon=True)
            closer.start()
            closer.join(config['stop_timeout_seconds'] / 2)
            if closer.is_alive():
                recorder.fail('md_stop_timeout')
        recorder.stop(config['stop_timeout_seconds'] / 2)
        if not recorder.committed or not all(api.tick_counts.values()) or set(api.confirmed) != set(recorder.contracts):
            recorder.fail('incomplete_subscription_or_no_ticks')
        status = recorder.write_status(api.poll(allow_requests=False), 'FAILED' if recorder.error else
                                       ('COMPLETED_WITH_GAPS' if recorder.dropped or api.disconnects else 'COMPLETED'))
        print(json.dumps(status, ensure_ascii=False), flush=True)
    # No background/native destructor may make this process hang after the final status.
    return 1 if recorder.error else 0


def supervise(args, config: dict) -> int:
    run_id = new_run_id()
    cmd = [sys.executable, str(Path(__file__).resolve()), '--worker', '--env', args.env,
           '--config', str(args.config.resolve()), '--output', str(args.output.resolve()),
           '--env-file', str(args.env_file.resolve()), '--duration', str(args.duration),
           '--startup-timeout', str(args.startup_timeout), '--run-id', run_id]
    PRIVATE.mkdir(mode=0o700, exist_ok=True)
    print(f'只读行情采集启动 source={args.env} run_id={run_id} duration={args.duration}s', flush=True)
    child = subprocess.Popen(cmd, cwd=PRIVATE, start_new_session=True)
    started = time.monotonic()
    status_path = args.output / 'runs' / run_id / 'status.json'
    failed = None
    try:
        while child.poll() is None:
            time.sleep(0.25)
            if time.monotonic() - started > args.duration + config['stop_timeout_seconds'] + 15:
                failed = 'recorder_process_deadline'
                break
            if status_path.exists():
                status = json.loads(status_path.read_text())
                if time.time_ns() - status['heartbeat_epoch_ns'] > (config['writer_timeout_seconds'] + 5) * 1e9:
                    failed = 'recorder_process_heartbeat_timeout'
                    break
    except KeyboardInterrupt:
        child.send_signal(signal.SIGINT)
        try:
            child.wait(timeout=config['stop_timeout_seconds'] + 2)
        except subprocess.TimeoutExpired:
            failed = 'recorder_process_stop_timeout'
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
    if failed:
        print(f'采集器已结束: {failed}；最后一次 status 可能不完整', flush=True)
    if status_path.parent.exists():
        try:
            atomic_json(status_path.parent / 'supervisor.json', dict(
                run_id=run_id, child_returncode=child.returncode, error=failed,
                ended_epoch_ns=time.time_ns(), orderly=not failed and child.returncode == 0))
        except OSError:
            print('监督结果无法写盘；请以退出码和最后心跳判断完整性', flush=True)
    if failed:
        return 1
    print(f'采集结果目录: {status_path.parent.resolve()}', flush=True)
    return child.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description='Independent MD-only raw tick recorder')
    parser.add_argument('--env', choices=VARIANTS, required=True)
    parser.add_argument('--config', type=Path, default=ROOT / 'recorder.example.json')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--output', type=Path, default=ROOT / 'ctp-data')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--prepare', action='store_true', help='Prepare private native copy without connecting')
    modes.add_argument('--check', action='store_true', help='Check credentials and native isolation without connecting')
    modes.add_argument('--run', action='store_true', help='Connect MD only for a bounded recording')
    modes.add_argument('--recover-run', metavar='RUN_ID', help='Recover durable pending data offline; no CTP connection')
    modes.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--duration', type=float, default=120)
    parser.add_argument('--startup-timeout', type=float, default=60)
    parser.add_argument('--run-id', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.recover_run:
            result = recover_pending(args.output, args.env, args.recover_run)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result['recovered'] else 1
        config = load_config(args.config)
        if any(not math.isfinite(v) or v <= 0 for v in (args.duration, args.startup_timeout)):
            raise ValueError('运行与启动超时必须为有限正数')
        if args.prepare:
            print('采集原生副本已准备: ' + str(prepare_runtime(args.env)))
            return 0
        settings = load_md_settings(args.env, args.env_file)
        if args.check:
            _, native = load_md_api(args.env)
            print(json.dumps(dict(environment=args.env, credentials_present=True,
                                  native=native, contracts=config['contracts'], connected=False), ensure_ascii=False))
            return 0
        if args.worker:
            return worker(args, config, settings)
        if args.run:
            check_runtime(args.env)
            return supervise(args, config)
        print(json.dumps(dict(environment=args.env, config=config, connected=False), ensure_ascii=False))
        return 0
    except Exception as exc:
        # .env values and CTP authentication payloads must never enter public logs.
        print('采集未成功: ' + type(exc).__name__, file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    result = main()
    if '--worker' in sys.argv:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(result)
    raise SystemExit(result)
