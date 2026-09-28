"""Offline comparison only: fake order engine, separate synthetic recorder, no CTP connections."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))


def stress(output: Path, mode: str):
    from test_market_data_recorder import raw_tick, configuration
    from market_data_recorder import TickRecorder
    os.nice(10)
    recorder = TickRecorder(output, '7x24', configuration(flush_seconds=0.1, queue_size=1000))
    if mode == 'slow':
        write = recorder._write_partition
        def slow(partition, rows):
            time.sleep(0.5)
            write(partition, rows)
        recorder._write_partition = slow
    recorder.start({})
    (output / 'ready').touch()
    try:
        while not (output / 'stop').exists():
            for _ in range(20):
                recorder.on_tick(raw_tick(), 1)
            time.sleep(0.01)
    finally:
        recorder.stop(3)
        recorder.write_status({}, 'TEST_ONLY')


def replay():
    from dataclasses import asdict
    from test_session import start_session
    from test_ctp_adapter import FakeMainEngine
    from live_grid.audit import AuditWriter
    from live_grid.ctp_adapter import CtpLiveGridAdapter
    from live_grid.session import TickEvent, ClockEvent, OrderEvent, InterruptEvent, TradeEvent, PositionQueryCompleteEvent
    session = start_session()
    audit_temp = tempfile.TemporaryDirectory(prefix='recorder-audit-benchmark-')
    audit = AuditWriter(session.config, audit_temp.name)
    adapter = CtpLiveGridAdapter([session], {}, [audit])
    adapter.main_engine = FakeMainEngine()
    observations, latencies = [], []
    def consume(event):
        before = len(session.actions)
        start = time.perf_counter_ns()
        adapter._consume(event, session)
        latencies.append((time.perf_counter_ns() - start) / 1e6)
        observations.append(dict(state=session.state.value,
                                 actions=[asdict(a) for a in session.actions[before:]]))
    consume(TickEvent('rb2601', 'SHFE', 100, 99, 101, 0))
    consume(TickEvent('rb2601', 'SHFE', 100, 99, 101, .5))
    consume(ClockEvent(2))
    orders = [a for a in session.actions if a.kind == 'submit_order']
    assert len(orders) == 2, 'scenario must actually dispatch fake orders'
    for i, action in enumerate(orders, 1):
        consume(OrderEvent(str(i), 'rb2601', 'SHFE', action.payload['side'], 'NOTTRADED', 1,
                           client_id=action.payload['client_id']))
    for i in range(1000):
        consume(TickEvent('rb2601', 'SHFE', 100, 99, 101, 3 + i * 0.001))
    buy = next(a for a in orders if a.payload['side'] == 'BUY')
    buy_id = str(orders.index(buy) + 1)
    sell_id = '2' if buy_id == '1' else '1'
    consume(TradeEvent(buy_id, 'rb2601', 'SHFE', 'BUY', 1, 60, 'trade-1', client_id=buy.payload['client_id']))
    consume(OrderEvent(buy_id, 'rb2601', 'SHFE', 'BUY', 'ALLTRADED', 1, traded=1, client_id=buy.payload['client_id']))
    consume(ClockEvent(session._now + session.config.effective['closing_wait_seconds'] + .1))
    flatten = next(a for a in reversed(session.actions) if a.kind == 'submit_order' and a.payload['order_type'] == 'FAK')
    consume(OrderEvent('3', 'rb2601', 'SHFE', 'SELL', 'ALLTRADED', 1, traded=1, client_id=flatten.payload['client_id']))
    consume(TradeEvent('3', 'rb2601', 'SHFE', 'SELL', 1, 99, 'trade-2', client_id=flatten.payload['client_id']))
    consume(OrderEvent(sell_id, 'rb2601', 'SHFE', 'SELL', 'CANCELLED', 1))
    query = next(a for a in reversed(session.actions) if a.kind == 'query_position')
    consume(PositionQueryCompleteEvent(query.payload['request_id'], 'rb2601', 'SHFE', 0))
    consume(InterruptEvent())
    assert adapter.main_engine.sent and adapter.main_engine.cancelled
    assert session.final_net_position == 0
    fingerprint = hashlib.sha256(json.dumps(observations, sort_keys=True).encode()).hexdigest()
    audit.close()
    audit_temp.cleanup()
    return latencies, fingerprint


def compare(output: Path):
    reference = replay()[1]  # warmup and scenario qualification
    results = []
    # Alternate modes to avoid treating a single unusually fast baseline as representative.
    for round_no in range(3):
        for mode in ('baseline', 'normal', 'slow'):
            with tempfile.TemporaryDirectory(prefix='recorder-benchmark-') as tmp:
                root = Path(tmp)
                child = None
                try:
                    if mode != 'baseline':
                        child = subprocess.Popen([str(ROOT / '.venv-recorder/bin/python'), __file__,
                                                  '--stress', mode, '--output', str(root)])
                        deadline = time.monotonic() + 10
                        while not (root / 'ready').exists():
                            if child.poll() is not None or time.monotonic() > deadline:
                                raise RuntimeError('stress recorder failed to start')
                            time.sleep(.05)
                        time.sleep(.5)
                    samples = []
                    for _ in range(6):
                        times, fingerprint = replay()
                        assert fingerprint == reference, 'strategy actions or states changed'
                        samples.extend(times)
                    ordered = sorted(samples)
                    result = dict(round=round_no + 1, mode=mode, samples=len(samples),
                                  p50_ms=ordered[int(len(ordered)*.5)], p95_ms=ordered[int(len(ordered)*.95)],
                                  p99_ms=ordered[int(len(ordered)*.99)], max_ms=max(samples),
                                  action_state_sha256=reference)
                finally:
                    if child is not None:
                        (root / 'stop').touch()
                        try:
                            child.wait(timeout=6)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=2)
                if child is not None:
                    statuses = list(root.glob('runs/*/status.json'))
                    if statuses:
                        s = json.loads(statuses[0].read_text())
                        result['recorder_committed'] = s['committed_total']
                        result['recorder_dropped'] = s['dropped_total']
                        result['recorder_uncommitted'] = s['uncommitted_total']
                        result['recorder_error'] = s['error']
                results.append(result)
    passed = True
    for round_no in range(1, 4):
        group = [r for r in results if r['round'] == round_no]
        baseline = group[0]['p99_ms']
        passed &= all(r['p99_ms'] <= baseline + max(1, baseline * .05) for r in group[1:])
    report = dict(scope='offline adapter._consume with real AuditWriter disk flush and fake order engine; includes quote, fill, FAK, cancellation and reconciliation; excludes network, native callback and event-queue delay',
                  passed=passed, results=results)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    assert passed


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--stress', choices=['normal', 'slow'])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.stress:
        stress(args.output, args.stress)
    else:
        compare(args.output)
