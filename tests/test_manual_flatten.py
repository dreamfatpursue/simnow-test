"""Offline manual recovery: request, reconciliation, FAK, zero, cooldown, requalification."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_session import start_session, make_config, enter_flatten_timeout_hold, qualify_market
from live_grid.session import (
    ManualFlattenEvent, OrderQueryCompleteEvent, OrderEvent, PositionQueryCompleteEvent,
    ClockEvent, TickEvent, SessionState, InterruptEvent,
)
from live_grid.activity import ActivityIdentity
from trading_console import ConsoleState, ConsoleInputError


class ManualFlattenTests(unittest.TestCase):
    def test_opposing_positions_reject_manual_recovery_without_flatten_or_resume(self):
        session = start_session()
        enter_flatten_timeout_hold(session)
        resume_before = session._resume_quotes_at
        actions = session.handle(ManualFlattenEvent(session._now))
        query = next(a for a in actions if a.kind == 'query_position')
        actions = session.handle(PositionQueryCompleteEvent(
            query.payload['request_id'], 'rb2601', 'SHFE', 0, gross_position=2))

        self.assertEqual(session.state, SessionState.RISK_HOLD)
        self.assertEqual(session.failure_reason, 'dual_side_position')
        self.assertEqual(session.summary()['final_gross_position'], 2)
        self.assertIsNone(session._manual_recovery_request_id)
        self.assertFalse(session._manual_flatten)
        self.assertFalse(any(a.kind == 'submit_order' for a in actions))
        self.assertEqual(session._resume_quotes_at, resume_before)

    def test_missing_query_reply_times_out_without_order_and_allows_retry(self):
        from live_grid.ctp_adapter import CtpLiveGridAdapter, QUERY_RESPONSE_TIMEOUT_SECONDS
        from test_ctp_adapter import FakeMainEngine, RecordingAudit
        session = start_session()
        enter_flatten_timeout_hold(session)
        adapter = CtpLiveGridAdapter([session], {}, [RecordingAudit()])
        adapter.main_engine = FakeMainEngine()
        adapter.main_engine.gateway.query_order = lambda: 77
        clock = [1000.0]
        adapter._clock = lambda: clock[0]
        actions = session.handle(ManualFlattenEvent(session._now))
        adapter._dispatch(next(a for a in actions if a.kind == 'query_position'))
        self.assertEqual(adapter._inflight_order_queries, {77})
        self.assertIsNotNone(session._manual_recovery_request_id)
        clock[0] += QUERY_RESPONSE_TIMEOUT_SECONDS + 0.1
        adapter._expire_stalled_queries()
        self.assertFalse(adapter._inflight_order_queries)
        self.assertFalse(adapter._query_sent_at)
        self.assertIsNone(session._manual_recovery_request_id)
        self.assertEqual(session.state, SessionState.RISK_HOLD)
        self.assertFalse(adapter.main_engine.sent)
        retry = session.handle(ManualFlattenEvent(session._now))
        self.assertTrue(any(a.kind == 'query_position' for a in retry))
        self.assertFalse(any(a.kind == 'submit_order' for a in retry))
        adapter._on_order_query_complete(SimpleNamespace(data=SimpleNamespace(
            request_id=77, orders=(), error_id=0, error_msg='')))
        self.assertIsNotNone(session._manual_recovery_request_id)

    def test_disconnect_clears_inflight_query_without_creating_a_flatten_order(self):
        from live_grid.ctp_adapter import CtpLiveGridAdapter
        from test_ctp_adapter import FakeMainEngine, RecordingAudit
        session = start_session()
        enter_flatten_timeout_hold(session)
        adapter = CtpLiveGridAdapter([session], {}, [RecordingAudit()])
        adapter.main_engine = FakeMainEngine()
        adapter.main_engine.gateway.query_order = lambda: 81
        actions = session.handle(ManualFlattenEvent(session._now))
        adapter._dispatch(next(a for a in actions if a.kind == 'query_position'))
        adapter._on_connection(SimpleNamespace(data=SimpleNamespace(kind='trade', connected=False, reason='4097')))
        self.assertFalse(adapter._inflight_order_queries)
        self.assertIsNone(session._manual_recovery_request_id)
        self.assertEqual(session.state, SessionState.RISK_HOLD)
        self.assertFalse(adapter.main_engine.sent)

    def test_reconcile_lost_cancel_callbacks_then_latest_fak_and_five_second_wait(self):
        session = start_session(make_config(max_round_trips=5))
        enter_flatten_timeout_hold(session)
        # Reproduce this run: exchange knows cancelled, local callback was lost.
        for order in session._orders.values():
            if order.status == 'CANCELLED':
                order.status = 'NOTTRADED'
        requested_at = session._now
        actions = session.handle(ManualFlattenEvent(requested_at))
        query = next(a for a in actions if a.kind == 'query_position')
        self.assertFalse(session.handle(ManualFlattenEvent(requested_at)))
        self.assertFalse(session.handle(TickEvent('rb2601', 'SHFE', 80, 79, 81, requested_at)))
        session.handle(PositionQueryCompleteEvent('old-query', 'rb2601', 'SHFE', 0))
        self.assertIsNone(session.final_net_position)
        queried = tuple(OrderEvent(o.order_id, 'rb2601', 'SHFE', o.side,
                                  'ALLTRADED' if o.traded else 'CANCELLED', o.volume,
                                  traded=o.traded, client_id=o.client_id,
                                  offset='CLOSETODAY' if o.is_flatten else 'OPEN')
                        for o in session._orders.values())
        session.handle(OrderQueryCompleteEvent('orders', 'rb2601', 'SHFE', queried))
        self.assertFalse(session._active_orders())
        actions = session.handle(PositionQueryCompleteEvent(query.payload['request_id'], 'rb2601', 'SHFE', 1))
        flatten = next(a for a in actions if a.kind == 'submit_order')
        self.assertEqual((flatten.payload['price'], flatten.payload['order_type'], flatten.payload['volume']), (79, 'FAK', 1))
        closing = session.handle(OrderEvent('manual-close', 'rb2601', 'SHFE', 'SELL', 'ALLTRADED', 1,
                                           traded=1, client_id=flatten.payload['client_id']))
        query = next(a for a in closing if a.kind == 'query_position')
        session.handle(PositionQueryCompleteEvent(query.payload['request_id'], 'rb2601', 'SHFE', 0))
        zero_at = session._now
        self.assertEqual(session._round_trips, 1)
        self.assertEqual(session._resume_quotes_at, zero_at + 5)
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertFalse(qualify_market(session, start=zero_at + 1))
        self.assertFalse(session.handle(ClockEvent(zero_at + 5)))
        self.assertEqual(session._stable_tick_count, 0)
        actions = qualify_market(session, start=zero_at + 5)
        self.assertEqual(len([a for a in actions if a.kind == 'submit_order']), 2)

    def test_unknown_order_stale_quote_and_stop_remain_gates(self):
        for blocked in ('unknown', 'stale', 'stop', 'startup'):
            with self.subTest(blocked=blocked):
                session = start_session()
                if blocked == 'startup':
                    session.state = SessionState.RISK_HOLD
                    session.final_net_position = 1
                else:
                    enter_flatten_timeout_hold(session)
                if blocked == 'stop':
                    session.handle(InterruptEvent())
                at = session._now + (100 if blocked == 'stale' else 0)
                actions = session.handle(ManualFlattenEvent(at))
                if blocked in ('startup', 'stop'):
                    self.assertFalse(actions)
                    continue
                query = next(a for a in actions if a.kind == 'query_position')
                if blocked == 'unknown':
                    session.handle(OrderQueryCompleteEvent('orders', 'rb2601', 'SHFE', (
                        OrderEvent('foreign', 'rb2601', 'SHFE', 'SELL', 'NOTTRADED', 1, offset='CLOSE'),)))
                actions = session.handle(PositionQueryCompleteEvent(query.payload['request_id'], 'rb2601', 'SHFE', 1))
                self.assertFalse(any(a.kind == 'submit_order' for a in actions))
                self.assertEqual(session.state, SessionState.RISK_HOLD)

    def test_control_writes_only_target_marker_and_rejects_old_process_or_identity(self):
        with tempfile.TemporaryDirectory() as root:
            identity = ActivityIdentity('run-test', 4321, '2026-09-09', 'first', 'normal', 'hash',
                                        ('rb2601@SHFE', 'AP701@CZCE'), root)
            state = ConsoleState(root)
            directory = Path(root) / 'rb2601@SHFE'
            directory.mkdir()
            snapshot = {'contracts': [{'symbol': 'rb2601', 'exchange': 'SHFE', 'state': 'RISK_HOLD'}]}
            with patch.object(state, 'current_activity', return_value=identity), patch.object(state, 'current_overview', return_value=snapshot):
                with self.assertRaises(ConsoleInputError):
                    state.flatten('run-test', 'rb2601@SHFE')
                (directory / '.manual-flatten-supported').touch()
                with self.assertRaises(ConsoleInputError):
                    state.flatten('old-run', 'rb2601@SHFE')
                with self.assertRaises(ConsoleInputError):
                    state.flatten('run-test', '../other')
                state.flatten('run-test', 'rb2601@SHFE')
                state.flatten('run-test', 'rb2601@SHFE')
                self.assertTrue((directory / '.manual-flatten-requested').exists())
                self.assertFalse((Path(root) / 'AP701@CZCE' / '.manual-flatten-requested').exists())
                snapshot['contracts'][0]['state'] = 'FLATTENING'
                with self.assertRaises(ConsoleInputError):
                    state.flatten('run-test', 'rb2601@SHFE')

    def test_projection_keeps_manual_request_pending_until_result_or_failure(self):
        from live_grid.console_projection import AuditProjector
        identity = ActivityIdentity('run-test', 4321, '2026-09-09', 'first', 'normal', 'hash',
                                    ('rb2601@SHFE',), '/tmp/unused-audit')
        projector = AuditProjector(identity)
        key = 'rb2601@SHFE'
        def record(event_type, data, trace=()):
            projector._apply_record(key, {
                'at': 100.0, 'state_before': 'RISK_HOLD', 'state_after': 'RISK_HOLD',
                'event': {'type': event_type, 'data': data}, 'actions': [], 'trace': list(trace),
            })
        record('ManualFlattenEvent', {}, [{'code': 'manual_flatten_requested'}])
        self.assertTrue(projector._contracts[key]['manual_flatten_inflight'])
        record('ManualFlattenEvent', {}, [{'code': 'manual_flatten_rejected',
                                           'calculation': {'reason': 'manual_flatten_pending'}}])
        self.assertTrue(projector._contracts[key]['manual_flatten_inflight'])
        self.assertIsNone(projector._contracts[key]['risk_reason'])
        record('OrderQueryCompleteEvent', {}, [{'code': 'manual_flatten_query_failed'}])
        self.assertFalse(projector._contracts[key]['manual_flatten_inflight'])

    def test_control_deduplicates_accepted_request_while_query_is_pending(self):
        with tempfile.TemporaryDirectory() as root:
            identity = ActivityIdentity('run-test', 4321, '2026-09-09', 'first', 'normal', 'hash',
                                        ('rb2601@SHFE',), root)
            directory = Path(root) / 'rb2601@SHFE'
            directory.mkdir()
            (directory / '.manual-flatten-supported').touch()
            state = ConsoleState(root)
            snapshot = {'contracts': [{'symbol': 'rb2601', 'exchange': 'SHFE',
                                       'state': 'RISK_HOLD', 'manual_flatten_pending': True}]}
            with patch.object(state, 'current_activity', return_value=identity), patch.object(state, 'current_overview', return_value=snapshot):
                result = state.flatten('run-test', 'rb2601@SHFE')
            self.assertEqual(result['status'], 'pending')
            self.assertFalse((directory / '.manual-flatten-requested').exists())

    def test_recovery_queries_do_not_skip_order_reconciliation_or_overwrite_inflight_id(self):
        from test_ctp_adapter import make_adapter
        from live_grid.session import Action
        adapter, sessions, _ = make_adapter(('rb2601', 'SHFE'))
        gateway = adapter.main_engine.gateway
        gateway.query_order = lambda: 71
        key = ('rb2601', 'SHFE')
        request = Action('query_position', {'symbol': key[0], 'exchange': key[1],
                        'phase': 'recovery', 'request_id': 'recovery-manual-1'})
        adapter._dispatch(request)
        self.assertNotIn(key, adapter._recovery_position_pending)
        adapter._dispatch(Action('query_position', {**request.payload, 'request_id': 'recovery-2'}))
        self.assertEqual(adapter._recovery_order_pending[key], 'recovery-manual-1')
        with patch.object(adapter, '_consume'):
            adapter._on_order_query_complete(SimpleNamespace(data=SimpleNamespace(
                request_id=71, orders=(), error_id=0, error_msg='')))
        self.assertEqual(adapter._recovery_position_pending[key], 'recovery-manual-1')
        self.assertEqual(gateway.query_count, 1)

    def test_adapter_consumes_target_request_once_on_event_thread(self):
        from test_ctp_adapter import make_adapter
        adapter, sessions, audits = make_adapter(('rb2601', 'SHFE'), ('AP701', 'CZCE'))
        with tempfile.TemporaryDirectory() as root:
            adapter.run_audit = SimpleNamespace()
            for index, audit in enumerate(audits):
                audit.directory = Path(root) / str(index)
                audit.directory.mkdir()
            marker = audits[0].directory / '.manual-flatten-requested'
            marker.touch()
            with patch.object(adapter, '_consume') as consume:
                adapter._on_timer(None)
                adapter._on_timer(None)
                manual = [call for call in consume.call_args_list if isinstance(call.args[0], ManualFlattenEvent)]
                self.assertEqual(len(manual), 1)
                self.assertIs(manual[0].args[1], sessions[0])
            self.assertFalse(marker.exists())
