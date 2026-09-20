"""Synthetic provider boundaries for unit and isolated-image startup checks.

Never loaded by production. All outbound HTTP is replaced and recorded here.
"""
import hashlib
import io
import json
from datetime import date, datetime, timedelta
from pathlib import Path
import shutil
from urllib.parse import parse_qs, urlsplit
import zipfile
from zoneinfo import ZoneInfo

from danta.adapters import FetchResult, HttpResponse

NOW = datetime(2026, 9, 21, 16, 30, tzinfo=ZoneInfo('Asia/Seoul'))
CALLS = []
ACTIVE_ORDER = False


def archive(name, content):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as value:
        value.writestr(name, content)
    return output.getvalue()


def transport(method, url, headers=None, body=None, timeout=15):
    path, query = urlsplit(url).path, parse_qs(urlsplit(url).query, keep_blank_values=True)
    CALLS.append((method, path))
    if method == 'POST' and path != '/oauth2/tokenP':
        raise AssertionError('SYNTHETIC_STARTUP_MUST_NOT_SEND_ORDERS')
    data = {'rt_cd': '0'}
    if path == '/oauth2/tokenP':
        data = {'access_token': 'FAKE_TOKEN_NOT_A_CREDENTIAL',
                'access_token_token_expired': (NOW + timedelta(hours=12)).strftime('%Y-%m-%d %H:%M:%S')}
    elif path.endswith('chk-holiday'):
        start = date.fromisoformat(query['BASS_DT'][0])
        data['output'] = [{'bass_dt': (start + timedelta(days=i)).strftime('%Y%m%d'),
            'opnd_yn': 'Y' if (start + timedelta(days=i)).weekday() < 5 else 'N'} for i in range(40)]
    elif path.endswith('idxcode.mst.zip'):
        return HttpResponse(200, archive('idxcode.mst', b'00025' + b'FAKE_SECTOR'.ljust(40, b' ') + b'\n'))
    elif path.endswith('corpCode.xml'):
        return HttpResponse(200, archive('CORPCODE.xml', b'<result><list><corp_code>00000001</corp_code>'
            b'<corp_name>FAKE_COMPANY</corp_name><stock_code>005930</stock_code><modify_date>20260901</modify_date></list></result>'))
    elif path.endswith('list.json'):
        data = {'status': '013'}
    elif path.endswith(('inquire-daily-indexchartprice', 'inquire-daily-itemchartprice')):
        start, end = (date.fromisoformat(query[key][0]) for key in ('FID_INPUT_DATE_1', 'FID_INPUT_DATE_2'))
        dates = [start + timedelta(days=i) for i in range((end-start).days+1) if (start+timedelta(days=i)).weekday() < 5]
        index = path.endswith('inquire-daily-indexchartprice')
        names = ('bstp_nmix_hgpr','bstp_nmix_lwpr','bstp_nmix_prpr') if index else ('stck_hgpr','stck_lwpr','stck_clpr')
        data['output2'] = [{'stck_bsop_date': day.strftime('%Y%m%d'), names[0]: '20200', names[1]: '19800',
            names[2]: '20000', 'acml_tr_pbmn': '10000000000'} for day in reversed(dates[-100:])]
    elif path.endswith('inquire-balance'):
        data.update(output1=[{'pdno':'005930','hldg_qty':'10','ord_psbl_qty':'10','prpr':'20000','evlu_amt':'200000'}],
            output2=[{'prvs_rcdl_excc_amt':'1000000','tot_evlu_amt':'1200000','evlu_amt_smtl_amt':'200000',
                      'nass_amt':'1200000','tot_loan_amt':'0','cma_evlu_amt':'0'}])
    elif path.endswith('inquire-psbl-order'):
        data['output'] = {'nrcvb_buy_amt':'1000000','nrcvb_buy_qty':'50','ord_psbl_cash':'1000000'}
    elif path.endswith('inquire-daily-ccld'):
        data['output1'] = ([{'pdno':'005930','ord_dt':'20260921','odno':'1','orgn_odno':'0','ord_gno_brno':'001',
            'excg_id_dvsn_cd':'KRX','ord_dvsn_cd':'00','sll_buy_dvsn_cd':'02','ord_qty':'1','tot_ccld_qty':'0',
            'tot_ccld_amt':'0','cncl_cfrm_qty':'0','rmn_qty':'1','rjct_qty':'0'}] if ACTIVE_ORDER else [])
        data['output2'] = {}
    elif path.endswith(('inquire-psbl-rvsecncl', 'order-resv-ccnl')):
        data['output'] = []
    elif path.endswith('inquire-period-profit'):
        data.update(output1=[], output2={})
    else:
        raise AssertionError('UNEXPECTED_SYNTHETIC_ENDPOINT: ' + path)
    return HttpResponse(200, json.dumps(data).encode())


def install():
    """Replace provider boundaries only; config, authority, ledger and service run unchanged."""
    import danta.adapters as adapters
    import danta.adapters.kis as kis
    import danta.adapters.disclosures as dart
    import danta.deployment as deployment
    import danta.runtime as runtime
    adapters.http_transport = lambda **_: transport
    runtime.http_transport = lambda **_: transport
    kis.utcnow = dart.utcnow = lambda: NOW
    kis.KisAdapter.read_instruments = lambda _self, board: FetchResult(({
        'symbol':'005930','board':board,'group':'ST','industry':'0025','etp':'0','preferred':'0',
        'spac':'N','halted':'N','liquidation':'N','managed':'N'},) if board == 'KOSPI' else (), 'COMPLETE', NOW)
    kis.KisAdapter.subscribe_quotes = lambda *_args, **_kwargs: None
    kis.KisAdapter.stream_quote = lambda *_args: FetchResult((), 'FETCH_FAILED', NOW)
    deployment.model_evidence = lambda config, _secrets: {'source':'SYNTHETIC_ISOLATION_BOUNDARY',
        'isolation_verified':True, 'auth_home_env':'DANTA_CODEX_AUTH_HOME',
        'executable_sha256':hashlib.sha256(Path(shutil.which(config.app['model']['executable'])).read_bytes()).hexdigest()}
    original = deployment.prepare_application
    def prepared(source, **kwargs):
        kwargs.setdefault('clock', lambda: NOW)
        app = original(source, **kwargs)
        app.clock = lambda: NOW
        return app
    deployment.prepare_application = prepared
