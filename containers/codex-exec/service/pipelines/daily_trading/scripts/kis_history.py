#!/usr/bin/env python3
"""Research-only dated KIS history through the existing local MCP container."""
from __future__ import annotations

import argparse
from contextlib import AbstractContextManager
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import selectors
import sqlite3
import subprocess

try:
    from .market_scanner import normalized_date, utc_now
    from .dart_history import save_json
except ImportError:
    from market_scanner import normalized_date, utc_now
    from dart_history import save_json

# Only these two read-only tools are reachable; no credentials leave the container.
BRIDGE = r'''
import asyncio,json,logging,sys
from fastmcp import Client
logging.disable(logging.CRITICAL)
async def main():
    async with Client('http://127.0.0.1:3000/sse') as client:
        for line in sys.stdin:
            try:
                args=json.loads(line)
                if args['api_type'] not in ('inquire_daily_indexchartprice','inquire_daily_itemchartprice'):
                    raise ValueError()
                result=await client.call_tool('domestic_stock',args)
                body=result.structured_content
                if not body.get('ok') or not body.get('data',{}).get('success'):
                    raise ValueError()
                data=body['data']['data']
                if isinstance(data,str): data=json.loads(data)
                if str(data.get('rt_cd','0'))!='0' or not isinstance(data.get('output2'),list):
                    raise ValueError()
                print(json.dumps({'rows':data['output2']}),flush=True)
            except Exception:
                print(json.dumps({'error':'KIS read-only history request failed'}),flush=True)
asyncio.run(main())
'''

# Executed only inside the existing container; never import kis_auth (it can issue tokens).
DIRECT_BRIDGE = r'''
import json,logging,os,re,sys,time
from datetime import datetime
from pathlib import Path
import requests,yaml
logging.disable(logging.CRITICAL)
HOST='https://openapi.koreainvestment.com:9443'
ROUTES={
    'inquire_daily_indexchartprice':('/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice','FHKUP03500100','U'),
    'inquire_daily_itemchartprice':('/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice','FHKST03010100','J'),
}
MAX_PAYLOAD=2*1024*1024

class TransientHistoryError(RuntimeError):
    pass

def cached_credentials():
    key=os.environ.get('KIS_APP_KEY','')
    secret=os.environ.get('KIS_APP_SECRET','')
    if not key or not secret:
        raise ValueError()
    for path in sorted(Path('/root/KIS/config').glob('KISprod_*'),reverse=True):
        try:
            with path.open('rb') as source:
                raw=source.read(16385)
            if len(raw)>16384:
                continue
            cached=yaml.safe_load(raw)
            expires=cached['valid-date']
            if isinstance(expires,str):
                expires=datetime.strptime(expires,'%Y-%m-%d %H:%M:%S')
            token=cached['token']
            if isinstance(token,str) and token and expires>datetime.now():
                return key,secret,token,expires
        except Exception:
            continue
    raise ValueError()

def history_rows(args,session,credentials,timing):
    if not isinstance(args,dict) or set(args)!={'api_type','params'} or args['api_type'] not in ROUTES:
        raise ValueError()
    path,tr_id,market=ROUTES[args['api_type']]
    params=args['params']
    required={'env_dv','fid_cond_mrkt_div_code','fid_input_iscd','fid_input_date_1','fid_input_date_2','fid_period_div_code'}
    if market=='J': required.add('fid_org_adj_prc')
    if (not isinstance(params,dict) or set(params)!=required or params['env_dv']!='real'
            or params['fid_cond_mrkt_div_code']!=market or params['fid_period_div_code']!='D'
            or not re.fullmatch(r'[0-9A-Z]{6}' if market=='J' else r'[0-9]{4}',str(params['fid_input_iscd']))
            or (market=='J' and params['fid_org_adj_prc']!='1')):
        raise ValueError()
    start,end=(datetime.strptime(params[field],'%Y%m%d') for field in ('fid_input_date_1','fid_input_date_2'))
    if start>end:
        raise ValueError()
    key,secret,token,expires=credentials
    if expires<=datetime.now():
        raise ValueError()
    delay=0.5-(time.monotonic()-timing[0])
    if delay>0: time.sleep(delay)
    timing[0]=time.monotonic()
    headers={'authorization':'Bearer '+token,'appkey':key,'appsecret':secret,'tr_id':tr_id,'custtype':'P'}
    query={name.upper():value for name,value in params.items() if name!='env_dv'}
    with session.get(HOST+path,params=query,headers=headers,timeout=(5,25),allow_redirects=False,stream=True) as response:
        retry_after_present=bool(response.headers.get('Retry-After'))
        if response.status_code in (502,503,504) and not retry_after_present:
            raise TransientHistoryError()
        if response.status_code!=200:
            raise ValueError()
        payload=bytearray()
        for chunk in response.iter_content(65536):
            payload.extend(chunk)
            if len(payload)>MAX_PAYLOAD:
                raise ValueError()
        if any(value.encode() in payload for value in (key,secret,token)):
            raise ValueError()
        body=json.loads(payload)
    if isinstance(body,dict) and str(body.get('rt_cd'))!='0' and body.get('msg_cd')=='EGW00201':
        if retry_after_present:
            raise ValueError()  # Do not override a server-specified waiting period.
        raise TransientHistoryError()
    if not isinstance(body,dict) or str(body.get('rt_cd'))!='0':
        raise ValueError()
    rows=body.get('output2')
    if not isinstance(rows,list) or len(rows)>100 or not all(isinstance(row,dict) for row in rows):
        raise ValueError()
    # Decode escaped JSON before checking the only content allowed out of the container.
    encoded=json.dumps(rows,ensure_ascii=False).encode()
    if any(value.encode() in encoded for value in (key,secret,token)):
        raise ValueError()
    return rows

def main():
    try:
        credentials=cached_credentials()
    except Exception:
        print(json.dumps({'error':'KIS cached credentials unavailable or expired'}),flush=True)
        return
    with requests.Session() as session:
        session.trust_env=False
        timing=[0.0]
        for line in sys.stdin:
            try:
                rows=history_rows(json.loads(line),session,credentials,timing)
                print(json.dumps({'rows':rows}),flush=True)
            except requests.exceptions.SSLError:
                print(json.dumps({'error':'KIS read-only history request failed'}),flush=True)
            except (TransientHistoryError,requests.exceptions.Timeout,requests.exceptions.ConnectionError,requests.exceptions.ChunkedEncodingError):
                print(json.dumps({'error':'KIS transient history request failed','retryable':True}),flush=True)
            except Exception:
                print(json.dumps({'error':'KIS read-only history request failed'}),flush=True)

if __name__=='__main__':
    main()
'''


class TransientHistoryError(RuntimeError):
    """Only explicit transient bridge failures; never auth, malformed data or provenance."""


class KisHistoryClient(AbstractContextManager):
    def __init__(self, *, direct_rest=False):
        self.transport = 'direct_rest' if direct_rest else 'mcp'
        self.process = subprocess.Popen(
            ['docker', 'exec', '-i', 'kis-trade-mcp', '/app/.venv/bin/python', '-u', '-c', DIRECT_BRIDGE if direct_rest else BRIDGE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def request(self, kind, code, start, end):
        if kind not in {'index', 'stock'} or not re.fullmatch(r'[0-9A-Z]{4,6}', code):
            raise ValueError('invalid history target')
        params = {'env_dv': 'real', 'fid_cond_mrkt_div_code': 'U' if kind == 'index' else 'J',
                  'fid_input_iscd': code, 'fid_input_date_1': start.strftime('%Y%m%d'),
                  'fid_input_date_2': end.strftime('%Y%m%d'), 'fid_period_div_code': 'D'}
        if kind == 'stock':
            params['fid_org_adj_prc'] = '1'  # Raw execution prices, not today's adjusted series.
        api = 'inquire_daily_indexchartprice' if kind == 'index' else 'inquire_daily_itemchartprice'
        self.process.stdin.write(json.dumps({'api_type': api, 'params': params}) + '\n')
        self.process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(90):
                self.process.kill()
                raise RuntimeError('KIS history timeout')
        try:
            result = json.loads(self.process.stdout.readline())
        except ValueError:
            raise RuntimeError('KIS history transport ended') from None
        if 'error' in result:
            if result.get('retryable') is True:
                raise TransientHistoryError(result['error'])
            raise RuntimeError(result['error'])
        return result['rows']

    def __exit__(self, *exc):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()


def validate_page(rows, kind, start, end):
    valid = {}
    fields = ('bstp_nmix_oprc', 'bstp_nmix_hgpr', 'bstp_nmix_lwpr', 'bstp_nmix_prpr') if kind == 'index' else (
        'stck_oprc', 'stck_hgpr', 'stck_lwpr', 'stck_clpr')
    for row in rows:
        if not row.get('stck_bsop_date'):  # KIS sometimes pads the page with empty rows.
            if any(str(value).strip() for value in row.values()):
                raise ValueError('history row missing date')
            continue
        day = normalized_date(row['stck_bsop_date'])
        if not day or not start.isoformat() <= day <= end.isoformat():
            raise ValueError('history row outside requested dates')
        try:
            opening, high, low, close = [float(row[field]) for field in fields]
            volume = int(row['acml_vol'])
        except (KeyError, ValueError, TypeError):
            raise ValueError('invalid history numeric fields') from None
        if not all(math.isfinite(v) and v >= 0 for v in (opening, high, low, close)) or volume < 0:
            raise ValueError('invalid history numeric values')
        # Suspended stocks may have zero OHLC/volume; retain them but never mark executable.
        if volume > 0 and (low <= 0 or not low <= min(opening, close) <= max(opening, close) <= high):
            raise ValueError('invalid executable OHLC')
        if day in valid:
            raise ValueError('duplicate history date')
        valid[day] = row
    return valid


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS history_bars (
          kind TEXT, code TEXT, trade_date TEXT, row_json TEXT NOT NULL,
          PRIMARY KEY(kind,code,trade_date));
        CREATE TABLE IF NOT EXISTS history_pages (
          kind TEXT, code TEXT, requested_start TEXT, requested_end TEXT,
          rows INTEGER, sha256 TEXT, collected_at TEXT,
          PRIMARY KEY(kind,code,requested_start,requested_end));
        CREATE TABLE IF NOT EXISTS history_collection (
          kind TEXT, code TEXT, requested_start TEXT, requested_end TEXT,
          status TEXT, next_end TEXT, error TEXT,
          PRIMARY KEY(kind,code,requested_start,requested_end));
    ''')


def save_calendar(path, dates, other):
    if (dates != other or len(dates) != 1473 or dates != sorted(set(dates))
            or dates[0] != '2020-01-02' or dates[-1] != '2025-12-30'):
        raise ValueError('full 2020-2025 KOSPI/KOSDAQ calendar incomplete or inconsistent')
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8')).get('trading_dates') != dates:
            raise ValueError('saved research calendar differs; do not overwrite dataset scope')
        return  # Preserve the provenance timestamp/hash used by a resumable universe database.
    save_json(path, {'trading_dates': dates, 'source': 'KIS dated output2, KOSPI/KOSDAQ agree', 'collected_at': utc_now()})


def collect_series(client, db, kind, code, start, end):
    key = (kind, code, start.isoformat(), end.isoformat())
    old = db.execute('SELECT status,next_end FROM history_collection WHERE kind=? AND code=? AND requested_start=? AND requested_end=?', key).fetchone()
    if old and old[0] == 'complete':
        return
    cursor = date.fromisoformat(old[1]) if old else end
    db.execute('INSERT OR REPLACE INTO history_collection VALUES (?,?,?,?,?,?,NULL)', (*key, 'partial', cursor.isoformat()))
    db.commit()
    try:
        for _ in range(40):
            rows = client.request(kind, code, start, cursor)
            page = validate_page(rows, kind, start, cursor)
            previous = min(page) if page else None
            next_end = date.fromisoformat(previous) - timedelta(days=1) if previous else start - timedelta(days=1)
            with db:
                for day, row in page.items():
                    payload = json.dumps(row, sort_keys=True, separators=(',', ':'))
                    existing = db.execute('SELECT row_json FROM history_bars WHERE kind=? AND code=? AND trade_date=?', (kind, code, day)).fetchone()
                    if existing and existing[0] != payload:
                        raise ValueError('conflicting cached history row')
                    db.execute('INSERT OR IGNORE INTO history_bars VALUES (?,?,?,?)', (kind, code, day, payload))
                db.execute('INSERT OR REPLACE INTO history_pages VALUES (?,?,?,?,?,?,?)',
                           (kind, code, start.isoformat(), cursor.isoformat(), len(page),
                            hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(), utc_now()))
                done = next_end < start
                db.execute('UPDATE history_collection SET status=?,next_end=?,error=NULL WHERE kind=? AND code=? AND requested_start=? AND requested_end=?',
                           ('complete' if done else 'partial', next_end.isoformat(), *key))
            if done:
                return
            cursor = next_end
        raise ValueError('history page limit exceeded')
    except Exception as exc:
        with db:
            db.execute('UPDATE history_collection SET status=?,error=? WHERE kind=? AND code=? AND requested_start=? AND requested_end=?', ('failed', type(exc).__name__, *key))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--start', type=date.fromisoformat, default=date(2019, 1, 1))
    parser.add_argument('--end', type=date.fromisoformat, default=date(2025, 12, 31))
    parser.add_argument('--calendar', type=Path)
    parser.add_argument('--universe', type=Path)
    parser.add_argument('--limit-symbols', type=int)
    parser.add_argument('--direct-rest', action='store_true', help='Reuse existing container credentials/token for direct read-only KIS REST')
    args = parser.parse_args()
    if args.start > args.end:
        parser.error('start must be before end')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.output) as db, KisHistoryClient(direct_rest=args.direct_rest) as client:
        print(f'history transport={client.transport}; source=KIS dated output2', flush=True)
        initialize(db)
        for code in ('0001', '1001'):
            collect_series(client, db, 'index', code, args.start, args.end)
            print(f'index {code} collected', flush=True)
        if args.calendar:
            dates = [row[0] for row in db.execute("SELECT trade_date FROM history_bars WHERE kind='index' AND code='0001' AND trade_date BETWEEN '2020-01-01' AND '2025-12-31' ORDER BY trade_date")]
            other = [row[0] for row in db.execute("SELECT trade_date FROM history_bars WHERE kind='index' AND code='1001' AND trade_date BETWEEN '2020-01-01' AND '2025-12-31' ORDER BY trade_date")]
            save_calendar(args.calendar, dates, other)
            print(f'calendar {len(dates)} dates', flush=True)
        if args.universe:
            with sqlite3.connect(f'file:{args.universe}?mode=ro', uri=True) as universe:
                codes = [row[0] for row in universe.execute('SELECT DISTINCT code FROM universe_membership ORDER BY code')]
            if args.limit_symbols:
                codes = codes[:args.limit_symbols]
            for index, code in enumerate(codes, 1):
                collect_series(client, db, 'stock', code, args.start, args.end)
                print(f'stock {index}/{len(codes)} {code} collected', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
