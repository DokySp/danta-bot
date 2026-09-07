#!/usr/bin/env python3
"""Validate/join dated research inputs. No orders, model calls or backtest."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3

try:
    from . import dart_history
except ImportError:
    import dart_history


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)


def load_financials(roots):
    """Reparse verified archives; do not trust previously calculated growth flags."""
    records = {}
    known_filings = {}
    paths = sorted({path for root in roots for path in Path(root).rglob('*.json')})
    identity_fields = ('corp_code', 'stock_code', 'rcept_no', 'report_code', 'period_end', 'received_on', 'available_on')
    for path in paths:
        if not path.name.startswith('filings-'):
            continue
        for row in json.loads(path.read_text(encoding='utf-8')):
            canonical = dart_history.filing_metadata({
                'corp_code': row['corp_code'], 'stock_code': row['stock_code'],
                'rcept_no': row['rcept_no'], 'rcept_dt': row['received_on'].replace('-', ''),
                'report_nm': row['report_name']})
            # rcept_no is an identifier, not the official received date. Real
            # list.json receipts differ in either direction; retain rcept_dt.
            if canonical is None or any(canonical.get(k) != row.get(k) for k in identity_fields):
                raise ValueError('invalid source filing date/period/identity')
            key = (row['corp_code'], row['rcept_no'])
            if key in known_filings and any(known_filings[key].get(k) != row.get(k) for k in identity_fields):
                raise ValueError('conflicting source filing identity')
            known_filings[key] = row
    for path in paths:
        match = re.fullmatch(r'(\d{14})(?:\.instance)?\.json', path.name)
        if not match:
            continue
        row = json.loads(path.read_text(encoding='utf-8'))
        if (row.get('rcept_no') != match[1] or not re.fullmatch(r'\d{8}', row.get('corp_code', ''))
                or row.get('available_on') != (date.fromisoformat(row['received_on']) + timedelta(days=1)).isoformat()):
            raise ValueError('invalid financial source identifiers/cutoff')
        key = (row['corp_code'], row['rcept_no'])
        listed = known_filings.get(key)
        if listed is None or any(listed.get(k) != row.get(k) for k in identity_fields):
            raise ValueError('financial sidecar differs from source filing identity')
        row.pop('financial_data_issue', None)  # Recompute, never trust a sidecar's quality flag.
        if row.get('archive'):
            name = row['archive']
            if name != path.with_suffix('.zip').name:
                raise ValueError('financial archive receipt/name mismatch')
            payload = (path.parent / name).read_bytes()
            compact = row.get('storage_mode') == 'instance_only'
            if (compact != path.name.endswith('.instance.json')
                    or row.get('storage_mode') not in (None, 'instance_only')
                    or not re.fullmatch(r'[0-9a-f]{64}', str(row.get('sha256', '')))):
                raise ValueError('financial archive storage/provenance mismatch')
            expected_hash = row.get('stored_sha256') if compact else row.get('sha256')
            if hashlib.sha256(payload).hexdigest() != expected_hash:
                raise ValueError('financial archive hash mismatch')
            if compact:
                name, instance = dart_history.xbrl_instance(payload)
                if name != row.get('instance_name') or hashlib.sha256(instance).hexdigest() != row.get('instance_sha256'):
                    raise ValueError('financial instance provenance mismatch')
            try:
                row['facts'] = dart_history.consolidated_facts(payload, row['corp_code'])
            except dart_history.ConflictingFacts as exc:
                row['facts'] = []
                row['financial_data_issue'] = str(exc)
        else:
            row['facts'] = []
        row['research_growth'] = dart_history.research_growth(row['facts'], row['period_end'])
        if key in records and (row.get('sha256'), row['facts']) != (records[key].get('sha256'), records[key]['facts']):
            raise ValueError('conflicting financial source copies')
        records[key] = row
    # A listed but not yet downloaded latest receipt must suppress an older positive signal.
    for key, row in known_filings.items():
        if key not in records:
            records[key] = {**row, 'facts': [], 'research_growth': dart_history.research_growth([], row['period_end'])}
    return list(records.values())


def join_day(universe, prices, records, decision_date):
    """Research candidates only; held delisted assets require a separate action ledger."""
    cutoff = decision_date.isoformat()
    previous = prices.execute("SELECT MAX(trade_date) FROM history_bars WHERE kind='index' AND code='0001' AND trade_date<?", (cutoff,)).fetchone()[0]
    complete = universe.execute("SELECT COUNT(*) FROM universe_snapshots WHERE snapshot_date=? AND status='complete'", (previous,)).fetchone()[0]
    if complete != 2:
        return {'decision_date': cutoff, 'status': 'unavailable', 'reason': 'previous-session membership incomplete', 'candidates': []}
    by_code = defaultdict(list)
    for row in records:
        code = str(row.get('stock_code', '')).strip()
        if re.fullmatch(r'[0-9A-Z]{6}', code):
            by_code[code].append(row)
    candidates = []
    expected = [r[0] for r in prices.execute("SELECT trade_date FROM history_bars WHERE kind='index' AND code='0001' AND trade_date<? ORDER BY trade_date DESC LIMIT 60", (cutoff,))]
    for code, market in universe.execute('SELECT code,market FROM universe_membership WHERE snapshot_date=? ORDER BY code', (previous,)):
        company_records = by_code[code]
        if len({row['corp_code'] for row in company_records}) > 1:
            raise ValueError('historical code/company identity needs reconciliation')
        financial = dart_history.latest_asof(company_records, decision_date, research=True)
        rows = prices.execute("SELECT trade_date,row_json FROM history_bars WHERE kind='stock' AND code=? AND trade_date<? ORDER BY trade_date DESC LIMIT 61", (code, cutoff)).fetchall()
        bars = [{'date': day, **json.loads(payload)} for day, payload in reversed(rows)]
        has_history = (len(bars) >= 60 and [b['date'] for b in bars][-60:] == sorted(expected)
                       and all(float(b['stck_clpr']) > 0 for b in bars[-60:]))
        # Raw corporate-action flags are evidence, not a verified quantity/cash ledger.
        action_flags = [b['date'] for b in bars if str(b.get('flng_cls_code', '')) not in ('', '00')
                        or str(b.get('revl_issu_reas', '')).strip()
                        or float(b.get('prtt_rate') or 0) != 0]
        signal = financial['research_growth'] if financial else {'status': 'unavailable', 'reason': 'no known filing'}
        candidates.append({'code': code, 'market': market, 'membership_date': previous,
                           'history_complete_60': has_history, 'bars': bars, 'corporate_action_flags': action_flags,
                           'financial': None if not financial else {k: financial.get(k) for k in (
                               'corp_code', 'rcept_no', 'period_end', 'received_on', 'available_on', 'sha256')},
                           'research_growth': signal})
    return {'decision_date': cutoff, 'status': 'joined_not_backtest_certified', 'membership_date': previous,
            'candidates': candidates}


def coverage(universe, prices, records, strategy):
    snapshots = dict(universe.execute('SELECT status,COUNT(*) FROM universe_snapshots GROUP BY status'))
    codes = {r[0] for r in universe.execute('SELECT DISTINCT code FROM universe_membership')}
    price_counts = dict(prices.execute("SELECT code,COUNT(*) FROM history_bars WHERE kind='stock' GROUP BY code"))
    financial_codes = {str(r.get('stock_code', '')) for r in records}
    finance_status = Counter(r['research_growth']['status'] for r in records)
    return {'strategy_id': strategy['id'], 'strategy_backtest_ready': False,
            'universe_snapshot_status': snapshots, 'observed_universe_codes': len(codes),
            'membership_rows': universe.execute('SELECT COUNT(*) FROM universe_membership').fetchone()[0],
            'price_symbols_with_any_bars': len(codes & price_counts.keys()),
            'price_symbols_without_bars': len(codes - price_counts.keys()),
            'stock_bar_rows': sum(price_counts.values()),
            'financial_receipts': len(records), 'financial_signal_status': dict(finance_status),
            'financial_quarantined_receipts': sum(bool(r.get('financial_data_issue')) for r in records),
            'universe_codes_with_any_filing': len(codes & financial_codes),
            'universe_codes_without_filing': len(codes - financial_codes),
            'not_certified': ['full price and filing range coverage including warmup and newly listed/delisted codes',
                              'historical code/company identity for unmapped codes',
                              'corporate-action cash/quantity ledger and causal signal adjustments',
                              'continuous backtest has not been run'],
            'orders': 0, 'trading_agent_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--universe', type=Path, required=True)
    parser.add_argument('--prices', type=Path, required=True)
    parser.add_argument('--dart-root', type=Path, action='append', default=[])
    parser.add_argument('--strategy', type=Path, default=Path(__file__).resolve().parent.parent / 'research-strategy-v1.json')
    parser.add_argument('--decision-date', type=date.fromisoformat)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    records = load_financials(args.dart_root)
    strategy = json.loads(args.strategy.read_text(encoding='utf-8'))
    with readonly(args.universe) as universe, readonly(args.prices) as prices:
        result = coverage(universe, prices, records, strategy)
        if args.decision_date:
            result['dated_inputs'] = join_day(universe, prices, records, args.decision_date)
    result['strategy_sha256'] = hashlib.sha256(args.strategy.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    dart_history.save_json(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k != 'dated_inputs'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
