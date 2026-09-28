"""Replay one saved run outside ctp-data; compare content and local query timings."""
import hashlib
import json
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from market_data_recorder import RAW_FIELDS, TICK_SCHEMA, TickRecorder, load_config

run_id = '20260914T025254-382ac215eb'
old_files = sorted((ROOT / 'ctp-data/raw_tick').rglob(f'part-{run_id}-*.parquet'))
before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in old_files}
original = sorted([r for p in old_files for r in pq.ParquetFile(p).read().to_pylist()], key=lambda r: r['seq_no'])
config = load_config(ROOT / 'recorder-guangfa-metals-chemicals.json') | {'flush_seconds': .01}

with tempfile.TemporaryDirectory(prefix='size-rotation-replay-') as directory:
    recorder = TickRecorder(Path(directory), 'guangfa', config)
    recorder.start({'test_only': True, 'replay_of': run_id})
    # Replay original 10-second receipt windows; each interval must be durably staged.
    groups = {}
    for row in original:
        window = (row['recv_epoch_ns'] - original[0]['recv_epoch_ns']) // 10_000_000_000
        groups.setdefault(window, []).append(row)
    for rows in groups.values():
        for row in rows:
            recorder.on_tick({k: row[k] for k in RAW_FIELDS}, row['connection_id'])
        deadline = time.monotonic() + 5
        while recorder.snapshot()['unsaved_total'] and time.monotonic() < deadline:
            time.sleep(.005)
        assert recorder.snapshot()['unsaved_total'] == 0
        assert not list(Path(directory).rglob('*.parquet')), 'timer must not finalize a file'
    assert recorder.stop(10), recorder.error
    new_files = sorted(Path(directory).rglob('*.parquet'))
    replayed = sorted([r for p in new_files for r in pq.ParquetFile(p).read().to_pylist()], key=lambda r: r['seq_no'])
    assert [{k: r[k] for k in RAW_FIELDS} for r in original] == [{k: r[k] for k in RAW_FIELDS} for r in replayed]
    columns = ['InstrumentID', 'LastPrice', 'Volume', 'BidPrice1', 'AskPrice1', 'seq_no']
    def query(files):
        start = time.perf_counter()
        table = ds.dataset([str(p) for p in files], format='parquet', schema=TICK_SCHEMA).to_table(
            columns=columns, filter=ds.field('InstrumentID') == 'SA701')
        return (time.perf_counter() - start) * 1000, table
    old_time, new_time = [], []
    for _ in range(7):
        elapsed, a = query(old_files)
        old_time.append(elapsed)
        elapsed, b = query(new_files)
        new_time.append(elapsed)
        assert a.sort_by('seq_no').equals(b.sort_by('seq_no'))
    result = dict(original_run_id=run_id, raw_fields_identical=True, rows=len(replayed),
                  rows_by_contract=dict(Counter(r['InstrumentID'] for r in replayed)),
                  old_files=len(old_files), new_files=len(new_files),
                  old_parquet_bytes=sum(p.stat().st_size for p in old_files),
                  new_parquet_bytes=sum(p.stat().st_size for p in new_files),
                  staging_database_bytes=(recorder.directory / 'pending.sqlite3').stat().st_size,
                  new_row_groups=sum(pq.ParquetFile(p).metadata.num_row_groups for p in new_files),
                  query_contract='SA701', query_rows=a.num_rows, query_columns=columns,
                  old_query_median_ms=statistics.median(old_time),
                  new_query_median_ms=statistics.median(new_time), repetitions=7,
                  caveat='Local warm-cache small-sample timing, not a production performance guarantee')
assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in old_files}
result['original_files_unchanged'] = True
(Path(__file__).parent / 'size-rotation-comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False, indent=2))
