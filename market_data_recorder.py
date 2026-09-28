"""Independent raw CTP recorder; no trading engine or order API imports."""
from __future__ import annotations

import json
import fcntl
import math
import os
import queue
import re
import resource
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

STRING_FIELDS = 'TradingDay reserve1 ExchangeID reserve2 UpdateTime ActionDay InstrumentID ExchangeInstID'.split()
FLOAT_FIELDS = '''LastPrice PreSettlementPrice PreClosePrice PreOpenInterest OpenPrice
HighestPrice LowestPrice Turnover OpenInterest ClosePrice SettlementPrice UpperLimitPrice
LowerLimitPrice PreDelta CurrDelta AveragePrice BandingUpperPrice BandingLowerPrice'''.split()
FLOAT_FIELDS += [f'{side}Price{level}' for side in ('Bid', 'Ask') for level in range(1, 6)]
INT_FIELDS = ['Volume'] + [f'{side}Volume{level}' for side in ('Bid', 'Ask') for level in range(1, 6)]
RAW_FIELDS = set(STRING_FIELDS + FLOAT_FIELDS + INT_FIELDS + ['UpdateMillisec'])
TICK_SCHEMA = pa.schema(
    [('schema_version', pa.int16()), ('source_id', pa.string()), ('run_id', pa.string()),
     ('connection_id', pa.int64()), ('seq_no', pa.int64()),
     ('recv_epoch_ns', pa.int64()), ('recv_monotonic_ns', pa.int64())]
    + [(name, pa.string()) for name in STRING_FIELDS]
    + [(name, pa.float64()) for name in FLOAT_FIELDS]
    + [(name, pa.int64()) for name in INT_FIELDS] + [('UpdateMillisec', pa.int32())])
DEFAULTS = dict(queue_size=10_000, target_file_mib=64, flush_seconds=10,
                min_free_gib=10, min_free_ratio=0.1, max_rss_mib=512,
                writer_timeout_seconds=30, stop_timeout_seconds=10)
EXCHANGES = {'SHFE', 'DCE', 'CZCE', 'CFFEX', 'INE', 'GFEX'}
# These bound serialization memory, not the number of rows in a file.
STAGE_BYTES = 1024**2
ROW_GROUP_BYTES = 4 * 1024**2


def load_config(path: Path) -> dict:
    data = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(data, dict) and 'flush_rows' in data:
        raise ValueError('flush_rows 已移除，请改用 target_file_mib（建议 64）')
    if not isinstance(data, dict) or set(data) - (set(DEFAULTS) | {'contracts'}):
        raise ValueError('采集配置含未知字段（不允许凭证）')
    contracts = data.get('contracts')
    if not isinstance(contracts, list) or not 1 <= len(contracts) <= 100:
        raise ValueError('contracts 必须包含 1–100 个明确订阅的合约')
    seen = set()
    for c in contracts:
        if not isinstance(c, dict) or set(c) != {'symbol', 'exchange'}:
            raise ValueError('每个合约仅允许 symbol/exchange')
        if not isinstance(c['symbol'], str) or not re.fullmatch(r'[A-Za-z0-9]{1,30}', c['symbol']):
            raise ValueError('合约代码不合法')
        if not isinstance(c['exchange'], str) or c['exchange'] not in EXCHANGES or c['symbol'] in seen:
            raise ValueError('交易所不合法或合约重复')
        seen.add(c['symbol'])
    result = DEFAULTS | data
    for name in DEFAULTS:
        value = result[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'{name} 必须为有限正数')
    for name in ('queue_size',):
        if not isinstance(result[name], int) or result[name] > 100_000:
            raise ValueError(f'{name} 必须为 1–100000 的整数')
    if result['min_free_ratio'] >= 1:
        raise ValueError('min_free_ratio 必须小于 1')
    if result['target_file_mib'] > 1024:
        raise ValueError('target_file_mib 不能大于 1024')
    if result['writer_timeout_seconds'] <= result['flush_seconds']:
        raise ValueError('writer_timeout_seconds 必须大于 flush_seconds')
    return result


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:10]


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, value: dict) -> None:
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, sort_keys=True, allow_nan=False)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    sync_directory(path.parent)


def append_json(path: Path, value: dict) -> None:
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')
        f.flush()
        os.fsync(f.fileno())


def rss_mib() -> float:
    # High-water mark is conservative: a previous memory spike stays visible.
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / (1024**2 if sys.platform == 'darwin' else 1024)


class TickRecorder:
    def __init__(self, root: Path, source_id: str, config: dict, run_id: str | None = None,
                 *, recovery: bool = False):
        if source_id not in {'first', '7x24', 'guangfa'}:
            raise ValueError('未知来源')
        self.root, self.source_id, self.config = Path(root), source_id, config
        self.run_id = run_id or new_run_id()
        if not re.fullmatch(r'[A-Za-z0-9_-]+', self.run_id):
            raise ValueError('run_id 不合法')
        self.directory = self.root / 'runs' / self.run_id
        if recovery:
            if not (self.directory / 'pending.sqlite3').is_file():
                raise ValueError('该运行没有可恢复的暂存库')
        else:
            self.directory.mkdir(parents=True, exist_ok=False)
        self.contracts = {c['symbol']: c['exchange'] for c in config['contracts']}
        self.queue = queue.Queue(config['queue_size'])
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.error: str | None = None
        self.accepting = False
        self.received = self.committed = self.dropped = self.batch_no = 0
        self.pending_durable = 0
        self.open_parts = {}
        self.part_prefix = f'part-{self.run_id}-{uuid.uuid4().hex[:8]}'
        self.drop_window: dict = {}
        self.heartbeat = time.monotonic()
        self.last_commit_at = None
        self.thread = threading.Thread(target=self._writer, name='raw-tick-writer', daemon=True)

    def start(self, metadata: dict) -> None:
        atomic_json(self.directory / 'manifest.json', dict(
            schema_version=1, source_id=self.source_id, run_id=self.run_id,
            config=self.config, python=sys.version.split()[0], pyarrow=pa.__version__,
            recv_clock='Python MdApi callback entry; not network arrival',
            started_epoch_ns=time.time_ns(), **metadata))
        atomic_json(self.directory / 'instruments.json', dict(
            source='subscription_config_only', metadata_queried=False,
            contracts=[dict(symbol=s, exchange=e, price_tick=None, volume_multiple=None)
                       for s, e in self.contracts.items()]))
        self.accepting = True
        self.thread.start()

    def on_tick(self, data: dict, connection_id: int) -> None:
        epoch, monotonic = time.time_ns(), time.monotonic_ns()
        with self.lock:
            if not self.accepting:
                return
            self.received += 1
            seq = self.received
            try:
                self.queue.put_nowait((dict(data), connection_id, seq, epoch, monotonic))
            except queue.Full:
                self.dropped += 1
                if not self.drop_window:
                    self.drop_window = dict(first_seq=seq, first_epoch_ns=epoch, count=0)
                self.drop_window.update(last_seq=seq, last_epoch_ns=epoch,
                                        count=self.drop_window['count'] + 1)

    def fail(self, reason: str) -> None:
        with self.lock:
            self.error = self.error or reason
            self.accepting = False
        self.stopping.set()

    def check_resources(self) -> None:
        disk = shutil.disk_usage(self.root)
        if disk.free < max(self.config['min_free_gib'] * 2**30, disk.total * self.config['min_free_ratio']):
            raise RuntimeError('disk_reserve_reached')
        if rss_mib() > self.config['max_rss_mib']:
            raise RuntimeError('memory_limit_reached')

    def snapshot(self) -> dict:
        with self.lock:
            return dict(source_id=self.source_id, run_id=self.run_id,
                        received_total=self.received, committed_total=self.committed,
                        dropped_total=self.dropped,
                        uncommitted_total=self.received - self.committed - self.dropped,
                        pending_durable_total=self.pending_durable,
                        unsaved_total=self.received - self.committed - self.dropped - self.pending_durable,
                        queue_depth=self.queue.qsize(), error=self.error,
                        writer_heartbeat_monotonic=self.heartbeat,
                        last_commit_epoch_ns=self.last_commit_at, rss_high_water_mib=rss_mib())

    def write_status(self, connection: dict, state: str) -> dict:
        with self.lock:
            gaps, self.drop_window = self.drop_window, {}
        if gaps:
            append_json(self.directory / 'gaps.jsonl', dict(
                kind='queue_overflow', bounds_may_include_retained_ticks=True, **gaps))
        status = self.snapshot() | connection | dict(state=state, heartbeat_epoch_ns=time.time_ns())
        status['disk_free_bytes'] = shutil.disk_usage(self.root).free
        atomic_json(self.directory / 'status.json', status)
        return status

    def stop(self, timeout: float | None = None) -> bool:
        with self.lock:
            self.accepting = False
        self.stopping.set()
        if self.thread.ident is not None:
            self.thread.join(self.config['stop_timeout_seconds'] if timeout is None else timeout)
            if self.thread.is_alive():
                self.fail('writer_stop_timeout')
        return not self.error

    def _row(self, item: tuple) -> tuple[tuple[str, str], dict]:
        data, connection, seq, epoch, monotonic = item
        if set(data) - RAW_FIELDS or data.get('InstrumentID') not in self.contracts:
            raise ValueError('raw_schema_mismatch')
        # Reject lossy input coercion; do not normalize the raw observation.
        for name, value in data.items():
            if value is None:
                continue
            if name in STRING_FIELDS:
                valid = isinstance(value, str)
            elif name in INT_FIELDS or name == 'UpdateMillisec':
                bits = 32 if name == 'UpdateMillisec' else 64
                valid = type(value) is int and -(2 ** (bits - 1)) <= value < 2 ** (bits - 1)
            else:
                valid = type(value) is float or (type(value) is int and abs(value) <= 2**53)
            if not valid:
                raise ValueError('raw_field_type_mismatch')
        day = data.get('TradingDay', '')
        try:
            if not re.fullmatch(r'[0-9]{8}', day):
                raise ValueError()
            datetime.strptime(day, '%Y%m%d')
        except (ValueError, TypeError):
            day = 'UNKNOWN'
        exchange = data.get('ExchangeID') or self.contracts[data['InstrumentID']]
        if exchange not in EXCHANGES:
            exchange = 'UNKNOWN'
        return (day, exchange), dict(data, schema_version=1, source_id=self.source_id,
                                     run_id=self.run_id, connection_id=connection, seq_no=seq,
                                     recv_epoch_ns=epoch, recv_monotonic_ns=monotonic)

    def _write_partition(self, partition: tuple, rows: list[dict]) -> None:
        """Durably stage a batch; time flushing never closes a Parquet file."""
        self.check_resources()
        table = pa.Table.from_pylist(rows, schema=TICK_SCHEMA)
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, TICK_SCHEMA) as stream:
            stream.write_table(table)
        with self.db:
            self.db.execute('INSERT INTO batches(day, exchange, payload, bytes, rows) VALUES(?,?,?,?,?)',
                            (*partition, sink.getvalue().to_pybytes(), table.nbytes, len(rows)))
        with self.lock:
            self.pending_durable += len(rows)
        self._drain_partition(partition)

    def _new_part(self, partition: tuple) -> dict:
        day, exchange = partition
        directory = self.root / 'raw_tick' / f'source_id={self.source_id}' / f'trading_day={day}' / f'exchange={exchange}'
        directory.mkdir(parents=True, exist_ok=True)
        self.batch_no += 1
        final = directory / f'{self.part_prefix}-{self.batch_no:06d}.parquet'
        tmp = final.with_suffix('.tmp')
        return dict(final=final, tmp=tmp, rows=0, last_id=0,
                    writer=pq.ParquetWriter(tmp, TICK_SCHEMA, compression='zstd',
                                            compression_level=1, write_statistics=True))

    def _drain_partition(self, partition: tuple, final: bool = False) -> None:
        group_bytes = min(ROW_GROUP_BYTES, self.config['target_file_mib'] * 2**20)
        while True:
            self.heartbeat = time.monotonic()
            self.check_resources()
            part = self.open_parts.get(partition)
            after = part['last_id'] if part else 0
            available = self.db.execute('SELECT COALESCE(SUM(bytes),0) FROM batches '
                                       'WHERE day=? AND exchange=? AND id>?', (*partition, after)).fetchone()[0]
            if not available or (not final and available < group_bytes):
                break
            tables, ids, size = [], [], 0
            for batch_id, payload, nbytes in self.db.execute(
                    'SELECT id,payload,bytes FROM batches WHERE day=? AND exchange=? AND id>? ORDER BY id',
                    (*partition, after)):
                tables.append(pa.ipc.open_stream(payload).read_all())
                ids.append(batch_id)
                size += nbytes
                if size >= group_bytes:
                    break
            table = pa.concat_tables(tables)
            if part is None:
                part = self.open_parts[partition] = self._new_part(partition)
                part.update(first_id=ids[0], first_seq=table['seq_no'][0].as_py(),
                            first_recv_epoch_ns=table['recv_epoch_ns'][0].as_py())
            part['writer'].write_table(table, row_group_size=table.num_rows)
            part.update(last_id=ids[-1], rows=part['rows'] + table.num_rows,
                        last_seq=table['seq_no'][-1].as_py(),
                        last_recv_epoch_ns=table['recv_epoch_ns'][-1].as_py())
            # The size is compressed bytes already written. One group/footer may overshoot.
            if part['tmp'].stat().st_size >= self.config['target_file_mib'] * 2**20:
                self._finish_part(partition)
        if final and partition in self.open_parts:
            self._finish_part(partition)

    def _finish_part(self, partition: tuple) -> None:
        part = self.open_parts[partition]
        part['writer'].close()
        with part['tmp'].open('rb') as f:
            os.fsync(f.fileno())
        info = {k: part[k] for k in ('rows', 'first_seq', 'last_seq',
                                     'first_recv_epoch_ns', 'last_recv_epoch_ns')}
        info.update(file=str(part['final'].relative_to(self.root)), bytes=part['tmp'].stat().st_size)
        # Commit the publication intent before rename: recovery handles either side of the rename.
        with self.db:
            self.db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,0)',
                            (info['file'], *partition, part['first_id'], part['last_id'], json.dumps(info)))
        os.replace(part['tmp'], part['final'])
        # Rename-visible rows stay counted if the following manifest append fails.
        with self.lock:
            self.committed += part['rows']
            self.pending_durable -= part['rows']
            self.last_commit_at = time.time_ns()
        sync_directory(part['final'].parent)
        self._complete_publication(info, partition, part['first_id'], part['last_id'])
        del self.open_parts[partition]

    def _complete_publication(self, info: dict, partition: tuple, first: int, last: int) -> None:
        if info['file'] not in self.logged_parts:
            append_json(self.directory / 'parts.jsonl', info)
            self.logged_parts.add(info['file'])
        with self.db:
            self.db.execute('DELETE FROM batches WHERE day=? AND exchange=? AND id BETWEEN ? AND ?',
                            (*partition, first, last))
            self.db.execute('UPDATE publications SET completed=1 WHERE file=?', (info['file'],))

    def _open_spool(self) -> None:
        self.db = sqlite3.connect(self.directory / 'pending.sqlite3')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA auto_vacuum=FULL')
        self.db.execute('PRAGMA cache_size=-2048')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT, exchange TEXT, payload BLOB, bytes INTEGER, rows INTEGER);
            CREATE INDEX IF NOT EXISTS batch_partition ON batches(day,exchange,id);
            CREATE TABLE IF NOT EXISTS publications(file TEXT PRIMARY KEY, day TEXT, exchange TEXT,
                first_id INTEGER, last_id INTEGER, info TEXT, completed INTEGER);
        ''')
        log = self.directory / 'parts.jsonl'
        self.logged_parts = set()
        if log.exists():
            content = log.read_text()
            for line in content.splitlines():
                try:
                    self.logged_parts.add(json.loads(line)['file'])
                except (ValueError, KeyError):
                    # An interrupted append can leave a partial line; preserve it for audit.
                    continue
            if content and not content.endswith('\n'):
                with log.open('ab') as f:
                    f.write(b'\n')
        for file, day, exchange, first, last, encoded, done in self.db.execute(
                'SELECT * FROM publications').fetchall():
            info = json.loads(encoded)
            path = self.root / file
            if not path.resolve().is_relative_to((self.root / 'raw_tick').resolve()):
                raise ValueError('invalid_publication_path')
            if not done:
                if not path.exists():
                    # Only fully closed files have a publication intent.
                    if not path.with_suffix('.tmp').exists():
                        raise ValueError('missing_pending_publication')
                    if pq.ParquetFile(path.with_suffix('.tmp')).metadata.num_rows != info['rows']:
                        raise ValueError('invalid_pending_publication')
                    os.replace(path.with_suffix('.tmp'), path)
                sync_directory(path.parent)
                if pq.ParquetFile(path).metadata.num_rows != info['rows']:
                    raise ValueError('invalid_pending_publication')
                self._complete_publication(info, (day, exchange), first, last)
            if not path.is_file() or pq.ParquetFile(path).metadata.num_rows != info['rows']:
                raise ValueError('invalid_completed_publication')
            self.committed += info['rows']
        self.pending_durable = self.db.execute('SELECT COALESCE(SUM(rows),0) FROM batches').fetchone()[0]

    def _writer(self, recovery: bool = False) -> None:
        buffers = defaultdict(list)
        size, last_flush = 0, time.monotonic()
        item = None
        lease = None
        try:
            lease = (self.directory / 'writer.lock').open('a')
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._open_spool()
            if recovery:
                self.received = self.committed + self.pending_durable
            while not recovery and (not self.stopping.is_set() or not self.queue.empty()):
                self.heartbeat = time.monotonic()
                try:
                    item = self.queue.get(timeout=0.1)
                except queue.Empty:
                    item = None
                if item is not None:
                    partition, row = self._row(item)
                    buffers[partition].append(row)
                    size += 8 * len(row) + sum(len(v.encode('utf-8')) for v in row.values() if isinstance(v, str))
                if size >= STAGE_BYTES or time.monotonic() - last_flush >= self.config['flush_seconds']:
                    for partition, rows in buffers.items():
                        self._write_partition(partition, rows)
                    buffers.clear()
                    size, last_flush = 0, time.monotonic()
            for partition, rows in buffers.items():
                self._write_partition(partition, rows)
            for partition in self.db.execute('SELECT DISTINCT day,exchange FROM batches').fetchall():
                self._drain_partition(partition, final=True)
        except Exception as exc:
            if isinstance(exc, ValueError) and item is not None:
                # Preserve known scalar fields only; never dump unknown callback payloads.
                data = {k: (str(v) if isinstance(v, float) and not math.isfinite(v) else v)
                        for k, v in item[0].items() if k in RAW_FIELDS and
                        (v is None or type(v) in (str, int, float, bool))}
                try:
                    atomic_json(self.directory / 'quarantine.json', dict(
                        seq_no=item[2], raw_known_fields=data,
                        unknown_field_count=len(set(item[0]) - RAW_FIELDS),
                        nonfinite_floats_encoded_as_strings=True))
                except Exception:
                    pass  # The failed run and uncommitted count remain visible even on a full disk.
            # No arbitrary callback values or exception payloads in public logs.
            reason = str(exc) if type(exc) in (ValueError, RuntimeError) and str(exc) in {
                'raw_schema_mismatch', 'raw_field_type_mismatch', 'disk_reserve_reached', 'memory_limit_reached'
            } else type(exc).__name__
            self.fail('writer_failed:' + reason)
        finally:
            # Unpublished Parquet is disposable; durable batches remain available for recovery.
            for part in self.open_parts.values():
                try:
                    part['writer'].close()
                except Exception:
                    pass
            if hasattr(self, 'db'):
                self.db.close()
            if lease is not None:
                lease.close()


def recover_pending(root: Path, source_id: str, run_id: str) -> dict:
    """Offline conversion only. Original connection/failed-run status is never rewritten."""
    if not re.fullmatch(r'[A-Za-z0-9_-]+', run_id):
        raise ValueError('run_id 不合法')
    manifest = json.loads((Path(root) / 'runs' / run_id / 'manifest.json').read_text())
    if manifest['source_id'] != source_id:
        raise ValueError('恢复环境与原始运行不一致')
    recorder = TickRecorder(root, source_id, manifest['config'], run_id, recovery=True)
    recorder._writer(recovery=True)
    report = dict(run_id=run_id, recovered=not recorder.error,
                  parquet_rows=recorder.committed, pending_durable_rows=recorder.pending_durable,
                  error=recorder.error, upstream_completeness_verified=False)
    # Do not write into an active run when the writer lock is held.
    if recorder.error != 'writer_failed:BlockingIOError':
        atomic_json(recorder.directory / 'recovery.json', report)
    return report


def read_ticks(root: Path, source_id: str, trading_day: str, symbol: str) -> pa.Table:
    """Read finished files only; explicitly type Hive dates as strings."""
    import pyarrow.dataset as ds
    partitioning = ds.partitioning(pa.schema([
        ('source_id', pa.string()), ('trading_day', pa.string()), ('exchange', pa.string())]), flavor='hive')
    files = [str(p) for p in (Path(root) / 'raw_tick').rglob('*.parquet')]
    if not files:
        return pa.Table.from_pylist([], schema=TICK_SCHEMA)
    dataset = ds.dataset(files, format='parquet', partitioning=partitioning,
                         partition_base_dir=str(Path(root) / 'raw_tick'))
    return dataset.to_table(filter=(ds.field('source_id') == source_id)
                            & (ds.field('trading_day') == trading_day)
                            & (ds.field('InstrumentID') == symbol))
