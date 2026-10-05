"""One Korean-day journal collector for CLI, scheduled and Telegram reports."""
from datetime import datetime, time, timedelta, timezone
import json
from zoneinfo import ZoneInfo

from .config import digest
from .reporting import reported_fee


def collect_daily_report(app, *, now, day=None, session_id=None):
    report_at = app.bundle.calendar.session(session_id).opens_at if session_id else now
    day = day or report_at.astimezone(ZoneInfo('Asia/Seoul')).date()
    start = datetime.combine(day, time(), ZoneInfo('Asia/Seoul')).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    with app.store.lock:
        journal = [dict(row) for row in app.store.read(
            'SELECT created_at,run_id,kind,payload FROM journal WHERE created_at>=? AND created_at<? ORDER BY sequence',
            (start.isoformat(), end.isoformat()))]
        for row in journal:
            row['payload'] = json.loads(row['payload'])
            if row['kind'] == 'INTENT_RESERVED':
                intent = row['payload']
                intent['intent_id'] = digest([intent['plan_id'], intent['plan_revision'], intent['side']])
        touched = {row['payload']['intent_id'] for row in journal if row['payload'].get('intent_id')}
        touched.update(row['payload']['id'] for row in journal if row['kind'] == 'INTENT_RESERVED' and row['payload'].get('id'))
        orders, by_id = [], {}
        symbols = getattr(app.bundle, 'data', {}).get('instrument_names', {})
        for row in app.store.read('SELECT * FROM intents ORDER BY rowid'):
            raw = dict(row)
            intent = json.loads(raw['payload'])
            record = {key: raw[key] for key in ('instrument_id', 'side', 'quantity', 'state', 'cumulative_quantity', 'cumulative_notional')}
            record.update(reason=intent.get('reason'), run_id=intent.get('run_id'), name=symbols.get(raw['instrument_id']))
            by_id[raw['id']] = record
            if raw['id'] in touched:
                record['created_at'] = next((item['created_at'] for item in journal
                    if item['payload'].get('intent_id', item['payload'].get('id')) == raw['id']), None)
                orders.append(record)
        fills = []
        for row in journal:
            if row['kind'] in {'CUMULATIVE_FILL', 'FILL_CORRECTION'}:
                fill = row['payload']
                order = by_id.get(fill.get('intent_id'), {})
                fills.append({**{key: order.get(key) for key in ('instrument_id', 'name', 'side', 'reason')},
                    'at': fill.get('observed_at', row['created_at']), 'quantity': fill.get('quantity_delta'),
                    'amount_krw': fill.get('notional_delta_krw'), 'fee_krw': reported_fee(fill),
                    'correction': row['kind'] == 'FILL_CORRECTION'})
        state = app.status()
        for holding in state.get('holdings', []):
            holding['name'] = symbols.get(holding['instrument_id'])
            quote = getattr(app.bundle, 'quotes', {}).get(holding['instrument_id'])
            if quote:
                from .strategy import monitor_quote_max_age, quote_fresh
                if quote_fresh(quote, now, monitor_quote_max_age(app.profile)):
                    holding.update(price=str(quote.bid), value=str(quote.bid * holding['quantity']),
                        price_observed_at=quote.observed_at.isoformat(), valuation_quality='EXACT')
                else:
                    holding['valuation_quality'] = 'STALE'
        return {'schema_version': 1, 'created_at': now.isoformat(), 'date': day.isoformat(),
                'config_hash': app.config.config_hash, 'strategy_hash': app.config.strategy_hash,
                'code_id': app.code_id, 'status': state,
                'runs': [row['payload'] for row in journal if row['kind'] == 'RUN_OUTCOME'],
                'orders': orders, 'fills': fills, 'instruments': symbols,
                'nav': app.store.get('nav_points', []),
                'theses': [thesis.model_dump(mode='json') for thesis in app.theses()],
                'diagnostics': [{'at': row['created_at'], 'kind': row['kind'], **row['payload']} for row in journal
                    if row['kind'] in {'ACCOUNT_INCOMPLETE', 'ACCOUNT_RECOVERED', 'MONITOR_DEGRADED', 'MONITOR_RECOVERED', 'SERVICE_WORKER_FAILED', 'MODEL_OUTCOME'}]}
