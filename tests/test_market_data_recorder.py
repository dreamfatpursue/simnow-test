"""Run with .venv-recorder/bin/python -m unittest discover -s tests -p test_market_data_recorder.py -v."""
import json
import math
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import pyarrow.parquet as pq
except ImportError:
    raise unittest.SkipTest('Parquet recorder tests require the separate .venv-recorder')

from market_data_recorder import (DEFAULTS, FLOAT_FIELDS, INT_FIELDS, STRING_FIELDS,
                                  RAW_FIELDS, TickRecorder, load_config, read_ticks, recover_pending)
from run_market_recorder import MarketCallbacks, ROOT, load_md_settings, worker, supervise


def raw_tick(**changes):
    data = {name: '' for name in STRING_FIELDS}
    data.update({name: 0.0 for name in FLOAT_FIELDS})
    data.update({name: 0 for name in INT_FIELDS})
    data.update(InstrumentID='IF2610', ExchangeID='CFFEX', TradingDay='20260914',
                ActionDay='20260911', UpdateTime='21:00:00', UpdateMillisec=5,
                LastPrice=3500.0, Volume=10, Turnover=10500000.0)
    return data | changes


def configuration(**changes):
    return DEFAULTS | dict(contracts=[dict(symbol='IF2610', exchange='CFFEX')],
                           min_free_gib=0.001, min_free_ratio=0.000001,
                           flush_seconds=0.05, max_rss_mib=1024) | changes


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.recorders = []

    def tearDown(self):
        for recorder in self.recorders:
            recorder.stop(2)
        self.temp.cleanup()

    def recorder(self, **options):
        r = TickRecorder(self.root, '7x24', configuration(**options))
        self.recorders.append(r)
        return r

    def test_raw_roundtrip_preserves_clock_sentinels_duplicates_and_every_field(self):
        r = self.recorder()
        r.start({})
        tick = raw_tick(LastPrice=sys.float_info.max, BandingUpperPrice=math.inf, BidPrice5=math.nan)
        r.on_tick(tick, 1)
        r.on_tick(tick, 1)
        tick['LastPrice'] = 999  # callback must own a copy
        self.assertTrue(r.stop())
        rows = read_ticks(self.root, '7x24', '20260914', 'IF2610').sort_by('seq_no').to_pylist()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row['LastPrice'], sys.float_info.max)
            self.assertTrue(math.isinf(row['BandingUpperPrice']))
            self.assertTrue(math.isnan(row['BidPrice5']))
            self.assertEqual(row['ActionDay'], '20260911')
            self.assertEqual(row['UpdateMillisec'], 5)
        self.assertEqual([row['seq_no'] for row in rows], [1, 2])
        self.assertEqual(r.snapshot()['uncommitted_total'], 0)

    def test_invalid_partition_cannot_escape_and_original_is_preserved(self):
        r = self.recorder()
        r.start({})
        r.on_tick(raw_tick(TradingDay='../../escape', ExchangeID='../bad'), 1)
        r.stop()
        rows = read_ticks(self.root, '7x24', 'UNKNOWN', 'IF2610').to_pylist()
        self.assertEqual(rows[0]['TradingDay'], '../../escape')
        self.assertTrue(list(self.root.glob('raw_tick/**/exchange=UNKNOWN/*.parquet')))

    def test_missing_exchange_uses_config_without_rewriting_raw(self):
        r = self.recorder()
        r.start({})
        r.on_tick(raw_tick(ExchangeID=''), 1)
        r.stop()
        self.assertEqual(read_ticks(self.root, '7x24', '20260914', 'IF2610').to_pylist()[0]['ExchangeID'], '')
        self.assertTrue(list(self.root.glob('raw_tick/**/exchange=CFFEX/*.parquet')))

    def test_time_flush_is_durable_without_creating_small_parquet_files(self):
        r = self.recorder()
        r.start({})
        r.on_tick(raw_tick(), 1)
        limit = time.monotonic() + 3
        while not r.pending_durable and time.monotonic() < limit:
            time.sleep(0.01)
        self.assertEqual(r.pending_durable, 1)
        self.assertEqual(r.snapshot()['unsaved_total'], 0)
        self.assertFalse(list(self.root.rglob('*.parquet')))
        time.sleep(.15)
        self.assertFalse(list(self.root.rglob('*.parquet')))
        self.assertTrue(r.stop())
        self.assertEqual(r.committed, 1)

    def test_full_queue_never_waits_for_slow_disk_and_records_loss(self):
        r = self.recorder(queue_size=2)
        entered, release = threading.Event(), threading.Event()
        original = r._write_partition
        def blocked(partition, rows):
            entered.set()
            release.wait(3)
            original(partition, rows)
        with patch.object(r, '_write_partition', side_effect=blocked):
            r.start({})
            r.on_tick(raw_tick(), 1)
            self.assertTrue(entered.wait(2))
            start = time.monotonic()
            for _ in range(10):
                r.on_tick(raw_tick(), 1)
            self.assertLess(time.monotonic() - start, 0.2)
            self.assertEqual(r.dropped, 8)
            self.assertFalse(r.stop(0.01))
            release.set()
            r.stop(3)
        status = r.write_status({}, 'FAILED')
        self.assertEqual(status['received_total'], status['committed_total'] + status['dropped_total'])
        self.assertTrue((r.directory / 'gaps.jsonl').is_file())

    def test_writer_failure_stops_accepting_and_keeps_uncommitted_count(self):
        r = self.recorder()
        with patch('market_data_recorder.pq.ParquetWriter', side_effect=OSError('sensitive-payload')):
            r.start({})
            r.on_tick(raw_tick(), 1)
            r.stop()
        self.assertEqual(r.error, 'writer_failed:OSError')
        self.assertEqual(r.snapshot()['uncommitted_total'], 1)
        r.on_tick(raw_tick(), 1)
        self.assertEqual(r.received, 1)
        self.assertFalse(list(self.root.rglob('*.parquet')))
        self.assertEqual(r.snapshot()['pending_durable_total'], 1)
        report = recover_pending(self.root, '7x24', r.run_id)
        self.assertTrue(report['recovered'])
        self.assertEqual(read_ticks(self.root, '7x24', '20260914', 'IF2610').num_rows, 1)

    def test_committed_file_survives_manifest_failure_without_rewrite(self):
        r = self.recorder()
        with patch('market_data_recorder.append_json', side_effect=OSError('full')):
            r.start({})
            r.on_tick(raw_tick(), 1)
            r.stop()
        self.assertEqual(r.committed, 1)
        self.assertIsNotNone(r.error)
        self.assertEqual(len(list(self.root.rglob('*.parquet'))), 1)
        self.assertEqual(read_ticks(self.root, '7x24', '20260914', 'IF2610').num_rows, 1)
        self.assertTrue(recover_pending(self.root, '7x24', r.run_id)['recovered'])
        self.assertTrue(recover_pending(self.root, '7x24', r.run_id)['recovered'])
        self.assertEqual(len(list(self.root.rglob('*.parquet'))), 1)
        self.assertEqual(len((r.directory / 'parts.jsonl').read_text().splitlines()), 1)

    def test_unknown_fields_and_wrong_types_are_visible_failures(self):
        for tick in (raw_tick(FutureSdkField=1), raw_tick(Volume=1.5), raw_tick(Volume=2**64)):
            r = self.recorder()
            r.start({})
            r.on_tick(tick, 1)
            self.assertFalse(r.stop())
            self.assertIn('mismatch', r.error)
            self.assertEqual(r.committed, 0)
            self.assertTrue((r.directory / 'quarantine.json').exists())

    def test_schema_covers_fields_from_project_native_callback(self):
        source = (ROOT / 'vendor/vnpy_ctp/vnpy_ctp/api/vnctp/vnctpmd/vnctpmd.cpp').read_text(errors='replace')
        body = source.split('void MdApi::processRtnDepthMarketData(Task *task)', 1)[1].split('void MdApi::processRtnForQuoteRsp', 1)[0]
        self.assertEqual(RAW_FIELDS, set(re.findall(r'data\["(\w+)"\]', body)))

    def test_disk_and_memory_guards(self):
        r = self.recorder(min_free_gib=1e10)
        with self.assertRaisesRegex(RuntimeError, 'disk_reserve'):
            r.check_resources()
        r.config['min_free_gib'] = 0.001
        with patch('market_data_recorder.rss_mib', return_value=1e10):
            with self.assertRaisesRegex(RuntimeError, 'memory_limit'):
                r.check_resources()

    def test_restart_does_not_overwrite_and_tmp_files_are_ignored(self):
        ids = []
        for _ in range(2):
            r = self.recorder()
            ids.append(r.run_id)
            r.start({})
            r.on_tick(raw_tick(), 1)
            r.stop()
        (self.root / 'raw_tick' / 'incomplete.tmp').write_bytes(b'broken footer')
        rows = read_ticks(self.root, '7x24', '20260914', 'IF2610').to_pylist()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['run_id'] for row in rows}, set(ids))

    def test_config_rejects_credentials_bad_symbols_and_unbounded_resources(self):
        path = self.root / 'config.json'
        for changes in ({'password': 'secret'}, {'queue_size': 0}, {'queue_size': True},
                        {'queue_size': 100001}, {'flush_seconds': float('nan')},
                        {'flush_rows': 5000}, {'target_file_mib': 0},
                        {'target_file_mib': True}, {'target_file_mib': float('inf')},
                        {'target_file_mib': 1025},
                        {'contracts': [dict(symbol='../bad', exchange='CFFEX')]}):
            path.write_text(json.dumps(configuration() | changes))
            with self.assertRaises(ValueError):
                load_config(path)

    def test_rotation_uses_bytes_and_tail_is_readable_with_no_gaps(self):
        r = self.recorder(target_file_mib=.02)
        r.start({})
        # Small byte budgets exercise several row groups and several physical files.
        with patch('market_data_recorder.STAGE_BYTES', 4000), patch('market_data_recorder.ROW_GROUP_BYTES', 8000):
            for i in range(400):
                r.on_tick(raw_tick(Volume=i, LastPrice=3500+i*.1), 1)
            self.assertTrue(r.stop())
        files = list(self.root.rglob('*.parquet'))
        self.assertGreater(len(files), 1)
        parts = [json.loads(x) for x in (r.directory / 'parts.jsonl').read_text().splitlines()]
        self.assertTrue(all(p['bytes'] >= .02 * 2**20 for p in parts[:-1]))
        rows = read_ticks(self.root, '7x24', '20260914', 'IF2610').sort_by('seq_no').to_pylist()
        self.assertEqual([x['seq_no'] for x in rows], list(range(1,401)))
        self.assertEqual([x['Volume'] for x in rows], list(range(400)))
        self.assertEqual(r.pending_durable, 0)
        self.assertFalse(list(self.root.rglob('*.tmp')))

    def test_recovery_rejects_active_writer(self):
        r = self.recorder()
        r.start({})
        r.on_tick(raw_tick(), 1)
        deadline = time.monotonic() + 3
        while not r.pending_durable and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(recover_pending(self.root, '7x24', r.run_id)['error'], 'writer_failed:BlockingIOError')
        self.assertFalse((r.directory / 'recovery.json').exists())
        self.assertTrue(r.stop())

    def test_process_crash_before_parquet_footer_recovers_exactly_once(self):
        code = '''
import os, sys, time
from pathlib import Path
sys.path.insert(0, 'tests')
from test_market_data_recorder import configuration, raw_tick
from market_data_recorder import TickRecorder
import market_data_recorder as module
module.ROW_GROUP_BYTES = 1
r = TickRecorder(Path(sys.argv[1]), '7x24', configuration(), 'crashed')
r.start({})
for i in range(20): r.on_tick(raw_tick(Volume=i), 1)
deadline = time.monotonic() + 5
while r.pending_durable < 20 and time.monotonic() < deadline: time.sleep(.01)
assert r.pending_durable == 20 and r.committed == 0
time.sleep(.1)
os._exit(9)
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.root)], cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 9, result.stderr)
        self.assertTrue(list(self.root.rglob('*.tmp')))
        for _ in range(2):
            report = recover_pending(self.root, '7x24', 'crashed')
            self.assertTrue(report['recovered'], report)
            self.assertEqual(report['parquet_rows'], 20)
        rows = read_ticks(self.root, '7x24', '20260914', 'IF2610').sort_by('seq_no').to_pylist()
        self.assertEqual([r['Volume'] for r in rows], list(range(20)))

    def test_failed_rename_recovers_durable_publication_intent(self):
        r = self.recorder()
        import os
        replace = os.replace
        def deny_parquet(source, target):
            if str(target).endswith('.parquet'):
                raise OSError('injected rename failure')
            return replace(source, target)
        with patch('market_data_recorder.os.replace', side_effect=deny_parquet):
            r.start({})
            r.on_tick(raw_tick(), 1)
            self.assertFalse(r.stop())
        self.assertEqual(r.pending_durable, 1)
        self.assertTrue(recover_pending(self.root, '7x24', r.run_id)['recovered'])
        self.assertEqual(read_ticks(self.root, '7x24', '20260914', 'IF2610').num_rows, 1)

    def test_multiple_days_and_exchanges_have_separate_tail_files(self):
        r = self.recorder(contracts=[dict(symbol='IF2610', exchange='CFFEX'),
                                     dict(symbol='SA701', exchange='CZCE')])
        r.start({})
        for day in ('20260914', '20260915'):
            r.on_tick(raw_tick(TradingDay=day), 1)
            r.on_tick(raw_tick(TradingDay=day, InstrumentID='SA701', ExchangeID='CZCE'), 1)
        self.assertTrue(r.stop())
        self.assertEqual(len(list(self.root.rglob('*.parquet'))), 4)
        for day in ('20260914', '20260915'):
            for symbol in ('IF2610', 'SA701'):
                self.assertEqual(read_ticks(self.root, '7x24', day, symbol).num_rows, 1)

    def test_md_settings_reads_only_selected_env_without_shell_execution(self):
        path = self.root / '.env'
        path.write_text('export CTP_USER_ID=someone\nCTP_PASSWORD="literal$(cmd) # text"\n'
                        'CTP_BROKER_ID=broker\nCTP_7X24_MARKET_FRONT=tcp://localhost:1234\n'
                        'CTP_GUANGFA_PASSWORD=other\n')
        settings = load_md_settings('7x24', path)
        self.assertEqual(settings['Password'], 'literal$(cmd) # text')
        self.assertEqual(set(settings), {'UserID', 'Password', 'BrokerID', 'address'})

    def test_md_login_subscription_reconnect_and_errors_without_td(self):
        class FakeMd:
            def __init__(self):
                self.calls = []
            def reqUserLogin(self, data, request_id):
                self.calls.append(('login', request_id))
                return 0
            def subscribeMarketData(self, symbol):
                self.calls.append(('subscribe', symbol))
                return 0
        class Api(MarketCallbacks, FakeMd):
            pass
        r = self.recorder()
        r.start({})
        api = Api(r, dict(UserID='u', Password='p', BrokerID='b'))
        api.onFrontConnected()
        api.poll()
        api.onRspUserLogin({}, {'ErrorID': 0}, 1, True)
        api.poll()
        api.onRspSubMarketData({'InstrumentID': 'IF2610'}, {}, 2, True)
        api.onRtnDepthMarketData(raw_tick())
        self.assertEqual(api.poll()['subscriptions_confirmed'], ['IF2610'])
        api.onFrontDisconnected(4097)
        api.onFrontConnected()
        api.next_login = 0
        api.poll()
        self.assertEqual(api.connection_id, 2)
        self.assertEqual(sum(call[0] == 'login' for call in api.calls), 2)
        api.onRspUserLogin({}, {'ErrorID': 3, 'ErrorMsg': 'secret'}, 2, True)
        self.assertEqual(api.poll()['md_error'], 'md_login_error:3')
        before = list(api.calls)
        api.poll(allow_requests=False)
        self.assertEqual(api.calls, before)
        r.stop()

    def test_private_native_load_does_not_import_trading_extension(self):
        if sys.platform != 'darwin' or not (ROOT / '.recorder-runtime/native').exists():
            self.skipTest('private macOS native runtime has not been prepared')
        code = "from run_market_recorder import load_md_api; import sys; cls, info = load_md_api('7x24'); assert not any('vnctptd' in k for k in sys.modules); assert '.recorder-runtime/native/' in info['loaded_md_library']; print('MD_ONLY_OK')"
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('MD_ONLY_OK', result.stdout)

    def test_complete_worker_lifecycle_uses_only_the_exposed_md_interface(self):
        calls = []
        class FakeNative:
            def __init__(self):
                calls.append('create_md')
            def createFtdcMdApi(self, path):
                calls.append(('flow', path))
            def registerFront(self, address):
                calls.append('md_front')
            def init(self):
                self.onFrontConnected()
            def reqUserLogin(self, data, reqid):
                self.onRspUserLogin({}, {}, reqid, True)
                return 0
            def subscribeMarketData(self, symbol):
                self.onRspSubMarketData(dict(InstrumentID=symbol), {}, 1, True)
                self.onRtnDepthMarketData(raw_tick())
                return 0
            def exit(self):
                calls.append('exit_md')
        args = SimpleNamespace(output=self.root, env='7x24', run_id='lifecycle',
                               duration=.1, startup_timeout=1)
        real_sleep = time.sleep
        with patch('run_market_recorder.load_md_api', return_value=(FakeNative, {})), \
             patch('run_market_recorder.PRIVATE', self.root / 'private'), \
             patch('run_market_recorder.os.nice'), \
             patch('run_market_recorder.time.sleep', side_effect=lambda _: real_sleep(.005)):
            self.assertEqual(worker(args, configuration(), dict(UserID='u', Password='secret', BrokerID='b', address='unused')), 0)
        status = json.loads((self.root / 'runs/lifecycle/status.json').read_text())
        self.assertEqual(status['committed_total'], 1)
        self.assertEqual(status['state'], 'COMPLETED')
        self.assertIn('exit_md', calls)
        self.assertTrue((self.root / 'runs/lifecycle/instruments.json').exists())
        self.assertFalse(any('secret' in p.read_text() for p in (self.root / 'runs/lifecycle').glob('*.json')))

    def test_supervisor_terminates_only_its_stuck_child(self):
        args = SimpleNamespace(env='7x24', config=self.root / 'config.json', output=self.root,
                               env_file=self.root / '.env', duration=.01, startup_timeout=.01)
        popen = subprocess.Popen
        children = []
        def launch(*args, **kwargs):
            child = popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            children.append(child)
            return child
        with patch('run_market_recorder.subprocess.Popen', side_effect=launch), \
             patch('run_market_recorder.PRIVATE', self.root / 'private'), \
             patch('run_market_recorder.time.monotonic', side_effect=[0, 100]), \
             patch('run_market_recorder.time.sleep'):
            self.assertEqual(supervise(args, configuration()), 1)
        self.assertIsNotNone(children[0].poll())


if __name__ == '__main__':
    unittest.main()
