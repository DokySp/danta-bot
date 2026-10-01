"""Deterministic JSON/HTML views of the same facts; no reporting model."""

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from .safety import reject_credentials


CSS = """
:root{color-scheme:light;font:16px/1.65 system-ui,sans-serif;color:#17293d;background:#f4f7fa}
body{max-width:1120px;margin:0 auto;padding:28px}main{padding:28px;background:white;border-radius:12px}
h1,h2,h3,h4{line-height:1.3;scroll-margin-top:20px}h2{margin-top:2.8em;border-bottom:2px solid #dbe6ef;padding-bottom:.5em}
a{color:#005a96;overflow-wrap:anywhere}p,li,td{overflow-wrap:anywhere}pre{overflow:auto;background:#edf2f7;padding:16px;border-radius:6px}
code{font-family:ui-monospace,monospace;font-size:.88em}table{border-collapse:collapse;display:block;overflow:auto;max-width:100%;font-size:.92em;margin:20px 0}
th,td{border:1px solid #cbd7e2;text-align:left;padding:9px;min-width:90px}th{background:#eaf1f8}
blockquote{border-left:4px solid #2b648d;margin-left:0;padding:4px 18px;background:#f1f6fa}
.metadata{font-size:.83em;color:#43586c;overflow-wrap:anywhere}.notice{padding:14px;background:#fff4d6;border-left:4px solid #9a6b00}
nav{background:#eaf1f8;padding:18px;border-radius:8px}nav ul{list-style:none;padding-left:12px}nav .level-3{margin-left:18px}
.report-nav{display:flex;gap:8px;flex-wrap:wrap;margin:22px 0;padding:12px}.report-nav a{padding:6px 12px;text-decoration:none;background:white;border-radius:6px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}.card{padding:18px;background:#f1f6fa;border-radius:8px}.card strong{display:block;font-size:1.3em}.muted{color:#53677c;font-size:.9em}
.report-table{width:100%}.report-table td{vertical-align:top}.report-table caption{text-align:left;color:#53677c}.report-table .number{white-space:nowrap;font-variant-numeric:tabular-nums}
.thesis{padding:18px;border:1px solid #dbe6ef;border-radius:8px;margin:14px 0}.thesis h3{margin-top:0}.thesis dl{display:grid;grid-template-columns:110px 1fr;gap:8px}.thesis dt{font-weight:600}.thesis dd{margin:0;overflow-wrap:anywhere}
.chart{width:100%;height:auto;display:block}.chart text{font:14px system-ui,sans-serif;fill:#43586c}details{margin:18px 0}summary{cursor:pointer;font-weight:600}.badge{display:inline-block;padding:2px 8px;background:#eaf1f8;border-radius:5px}
@media(max-width:650px){body{padding:10px}main{padding:16px}h1{font-size:1.6em}th,td{padding:7px}
.report-table,.report-table tbody{display:block;overflow:visible}.report-table thead{display:none}
.report-table tr{display:block;margin:12px 0;border:1px solid #cbd7e2;border-radius:8px;padding:8px}
.report-table td{display:grid;grid-template-columns:96px minmax(0,1fr);gap:10px;border:0;min-width:0}
.report-table td:before{content:attr(data-label);font-weight:600;color:#43586c}}
@media print{body{max-width:none;padding:0;background:white;font-size:10pt}main{padding:0}nav{display:none}pre{white-space:pre-wrap}table{display:table;font-size:8pt}tr,blockquote{break-inside:avoid}a{color:inherit}h2,h3{break-after:avoid}}
"""


def json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone aware")
        return value.astimezone(timezone.utc).isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(type(value).__name__)


def _document(title: str, body: str) -> str:
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{CSS}</style></head>'
            f'<body><main>{body}</main></body></html>')


def facts_html(data) -> str:
    if isinstance(data, dict):
        return '<table><tbody>' + ''.join(
            f'<tr><th scope="row">{html.escape(str(key))}</th><td>{facts_html(value)}</td></tr>'
            for key, value in data.items()) + '</tbody></table>'
    if isinstance(data, list):
        return '<ol>' + ''.join(f'<li>{facts_html(item)}</li>' for item in data) + '</ol>'
    if data is None:
        return '<span>미확인 / 자료 없음</span>'
    return html.escape(str(data))


MISSING = '미확인 / 자료 없음'
LABELS = {
    'status': '상태', 'run_status': '실행', 'decision_status': '투자 판단', 'order_status': '이번 검토의 주문',
    'model_status': '모델 실행', 'performance_status': '성과 검증', 'live_status': '실거래 권한',
    'reason': '이유', 'reasons': '이유', 'issues': '확인할 사항', 'error_type': '오류 종류', 'error': '오류',
    'mode': '운영 모드', 'paused': '신규 투자 일시정지', 'reconciled': '계좌 대조 완료', 'cash_krw': '전략 현금',
    'holdings': '보유 종목', 'working_orders': '진행 중인 주문', 'costs_complete': '거래 비용 확인 완료',
    'account_cash_reconciled': '현금 대조 완료', 'account_costs': '계좌 거래 비용', 'performance': '누적 성과',
    'nav_finalization': '마감 평가 확정', 'pretrade_cost_basis': '주문 전 비용 기준', 'provenance': '자료 출처',
    'authentication': 'Codex 인증', 'auth_status': 'Codex 인증', 'account_status': '계좌 조회',
    'monitor_status': '보호 감시', 'review_status': '투자 검토', 'scheduler_status': '예약 실행',
    'monitor_degraded': '보호 감시 장애', 'drawdown_paused': '낙폭 제한 일시정지', 'candidate_controls': '후보 종목 설정',
    'protection': '보호 감시·매도', 'model_called': '모델 호출', 'orders_created': '주문 생성',
    'holdings_changed': '보유 수량 변경', 'automatic_liquidation': '자동 청산', 'reexecute_trade': '거래 재실행',
    'instrument_id': '종목', 'name': '종목명', 'side': '방향', 'quantity': '수량', 'state': '주문 상태',
    'cumulative_quantity': '누적 체결 수량', 'quantity_delta': '추가 체결 수량', 'notional_delta_krw': '추가 체결 금액',
    'fee_delta_krw': '추가 수수료·세금', 'cumulative_notional': '누적 체결 금액', 'amount_krw': '체결 금액',
    'fee_krw': '수수료·세금', 'price': '현재가', 'mark': '평가 단가', 'value': '평가 금액',
    'average_entry': '평균 매입가', 'current_stop': '보호 기준가', 'initial_stop': '최초 보호 기준가',
    'created_at': '생성 시각', 'occurred_at': '발생 시각', 'at': '시각', 'as_of': '기준 시각', 'observed_at': '관측 시각',
    'updated_at': '갱신 시각', 'retry_at': '재시도 예정', 'expires_at': '만료 시각', 'resets_at': '한도 초기화',
    'completed': '마감 확정', 'quality': '자료 품질', 'valuation_quality': '평가 품질', 'coverage': '자료 완결성',
    'nav': '전략 자산', 'pnl': '누적 손익', 'twr': '누적 시간가중 수익률', 'max_drawdown': '최대 낙폭',
    'pnl_after_operating_cost': '운영 비용 차감 후 손익', 'net_external_flow': '순입출금',
    'operating_cost': '운영 비용', 'operating_cost_status': '운영 비용 확인', 'new_risk_allowed': '신규 위험 허용',
    'reasoning_effort': '추론 수준', 'current_effort': '현재 추론 수준', 'requested_effort': '요청 추론 수준',
    'changed': '변경 적용', 'controls': '후보 설정', 'removed': '후보에서 제거', 'excluded': '매수 제외',
    'economic_path': '투자 근거', 'horizon_case': '예상 전개', 'counterevidence': '반대 근거',
    'invalidation_case': '판단 무효 조건', 'exit_reason': '청산 이유', 'exited_at': '청산 시각',
    'max_holding_sessions': '최대 보유 거래일', 'planned_quantity': '계획 수량', 'risk_budget': '위험 예산',
    'origin': '보유 편입 경위', 'event_count': '수집한 공시 수', 'attempts': '최근 모델 호출',
    'returncode': '종료 코드', 'stderr': '실행 오류', 'diagnostic': '진단', 'diagnostics': '진단 기록',
    'total_tokens': '전체 토큰', 'input_tokens': '입력 토큰', 'output_tokens': '출력 토큰',
    'cached_input_tokens': '재사용 입력 토큰', 'usage': '토큰 사용량', 'usage_status': '사용량 조회',
    'kind': '종류', 'count': '건수', 'scope': '대상', 'source': '출처', 'version': '버전',
    'endpoint': '조회 항목', 'http_status': '서버 응답 코드', 'provider_code': '증권사 오류 코드',
    'provider_message': '증권사 응답', 'requested_at': '조회 시작 시각', 'elapsed_seconds': '소요 시간(초)',
    'method': '요청 방식', 'tr_id': '증권사 조회 코드', 'field': '응답 항목',
    'request_stage': '실패 단계',
    'transport_error': '통신 오류', 'failed_page': '실패한 조회 페이지', 'stable_seconds': '연속 정상 확인 시간(초)',
    'checked_at': '확인 시각', 'account_checked_at': '계좌 조회 시각', 'account_succeeded_at': '계좌 최근 성공',
    'monitor_checked_at': '감시 확인 시각', 'model_checked_at': '모델 최근 실행', 'model_id': '사용 모델',
    'model_purpose': '최근 모델 용도', 'chat_model': '일반 대화 AI', 'review_model': '투자 판단 AI',
    'status_checked_at': '상태 확인 시각', 'monitor_diagnostic': '현재 감시 문제', 'account_diagnostics': '현재 계좌 문제',
    'review_scope': 'AI 검토 범위', 'trigger': '검토 계기', 'review_targets': 'AI 검토 대상',
    'nav_risk_unverified': '현재 자산·위험 미검증', 'performance_uncertain': '성과 대조 미완료',
    'cash_reconciliation': '현금 차이 대조', 'pending_count': '미분류 현금 차이 건수',
    'adjustments': '현금 차이 기록', 'adjustment_id': '현금 차이 참조', 'blocked_reasons': '신규 투자 차단 이유',
    'complete': '자료 확인 완료',
}
STATES = {
    'NO_RECENT_EVENT_TO_REVIEW': '검토할 최근 공시가 없어 일봉 수집 대상에서 제외',
    'UNIVERSE_STATUS_OR_CLASSIFICATION_EXCLUDED': '보통주·정상거래·분류 확인 조건에 맞지 않아 제외',
    'EVENT_COVERAGE_PARTIAL': '공시 수집 또는 원문 확인이 불완전함',
    'PAGE_LIMIT': '공시 목록의 다음 페이지 수집이 필요함',
    'DOCUMENT_FETCH_FAILED': '공시 원문 응답을 읽지 못함',
    'DOCUMENT_NOT_AVAILABLE': '공시 제공처에 원본 파일이 없음 · 재확인 대기',
    'DART_SERVICE_UNAVAILABLE': '공시 제공처 서비스가 일시 중단됨',
    'REVALIDATING': 'AI 응답 완료 · 최신 조건 대조 중',
    'REVALIDATION_FAILED': 'AI 응답 후 최신 조건 대조 실패 · 주문 미실행',
    'STALE_DECISION': '판단 유효시간이 지났거나 공시 근거가 변경됨',
    'DECISION_EVIDENCE_REFRESH_INCOMPLETE': '판단 대상 공시의 최신 상태를 확인하지 못함',
    'DAILY_HISTORY_NOT_COLLECTED': '일봉 미수집으로 지표 검토 미진행',
    'NO_ELIGIBLE_CANDIDATES': '신규 후보가 사전 검사에서 모두 제외됨',
    'NO_REVIEW_TARGETS': '이번 범위에 AI가 검토할 후보·보유 종목 없음',
    'OUTSIDE_REVIEW_SCOPE': '이번 AI 검토 대상 밖 · 보유 현황 참고',
    'OPERATOR_EXCLUDED': '사용자 설정으로 신규 매수 제외',
    'PREFILTERED': 'AI 검토 전 제외', 'AWAITING_AI': 'AI 판단 미완료', 'NOT_REVIEWED': '검토 미완료',
    'ACCEPT': '매수 검토 승인', 'VETO': '매수 거부', 'WATCH': '관찰', 'INSUFFICIENT_DATA': '자료 부족',
    'ABSTAIN': '판단 유보', 'KEEP': '보유 유지',
    'STALE_OR_INVALID_QUOTE': '시세가 오래되었거나 유효하지 않음', 'MISSING_QUOTE': '시세 없음',
    'EXISTING_THESIS': '기존 보유 종목으로 추가 매수 제외', 'INELIGIBLE_UNIVERSE': '거래 대상 조건 불충족',
    'STALE_OR_INCOMPLETE_FEATURES': '일봉 자료가 오래되었거나 부족함', 'INSUFFICIENT_LIQUIDITY': '거래대금 기준 미달',
    'MISSING_EXECUTABLE_QUOTE': '매수·매도 호가 없음', 'SPREAD_TOO_WIDE': '호가 차이 한도 초과',
    'NO_VALID_RECENT_OFFICIAL_EVENT': '유효한 최근 공식 공시 근거 없음', 'TREND_GATE_FAILED': '이동평균 추세 조건 미달',
    'RELATIVE_STRENGTH_GATE_FAILED': '시장 대비 수익률 조건 미달', 'BOARD_INDEX_GATE_FAILED': '시장 지수 추세 조건 미달',
    'WAIT_PRICE': '추격 매수 가격 한도 초과', 'INVALID_INITIAL_STOP': '손절 가격 조건 불충족',
    'OUTSIDE_ENTRY_WINDOW': '신규 진입 시간 아님', 'NO_FEASIBLE_SIZE': '현금·위험·비중 한도 내 주문 수량 없음',
    'ROUNDTRIP_FRICTION_TOO_HIGH': '손절 위험 대비 거래비용 과다', 'SIZED': '주문 가능 수량 산정',
    'READY': '초기 검사 통과', 'SUCCESS': '성공', 'SUCCEEDED': '성공', 'COMPLETE': '완료',
    'RUNNING': '진행 중', 'FAILED': '실패', 'BLOCKED': '중단', 'UNKNOWN': '확인 불가',
    'UNCONFIRMED': '미확인', 'UNVERIFIED': '미검증', 'NONE': '없음', 'NOT_CALLED': '호출하지 않음',
    'NOT_REACHED': '판단 단계에 도달하지 못함', 'NO_CANDIDATES': '검토할 신규 후보 없음',
    'VALID': '판단 검증 통과', 'KEEP_EXISTING_PLAN': '기존 계획 유지', 'NEW_RISK_PAUSED': '신규 투자 일시정지',
    'WAITING_FOR_HUMAN': '운영자 확인 필요', 'PARTIAL': '일부 자료만 확인됨',
    'PAUSED': '신규 투자 일시정지', 'RESUMED': '신규 투자 재개', 'CONTINUES': '계속 작동',
    'SCHEDULE_ON': '예약 투자 검토 켜짐', 'SCHEDULE_OFF': '예약 투자 검토 꺼짐',
    'ACKNOWLEDGED': '증권사 주문 접수', 'PARTIALLY_FILLED': '일부 체결', 'FILLED': '전량 체결',
    'SUBMITTING': '주문 전송 중', 'PREPARED': '주문 준비', 'CANCEL_REQUESTED': '취소 요청 중',
    'CANCELLED': '취소 완료', 'CANCELED': '취소 완료', 'REJECTED': '거절', 'EXPIRED': '유효시간 만료',
    'BUY': '매수', 'SELL': '매도', 'MIXED': '여러 주문 상태', 'SHADOW_PLAN_ONLY': '관찰 모드의 계획만 생성',
    'FIXTURE_FILLED': '합성 데이터 체결', 'FIXTURE_RECORDED_RESPONSE': '합성 데이터의 모델 응답',
    'FIXTURE_ONLY': '합성 검증 자료', 'STRATEGY_UNPROVEN': '투자 전략 성과 미검증',
    'LIVE_NOT_AUTHORIZED': '실거래 권한 없음', 'OPERATOR_AUTHORIZED': '운영자가 허용한 범위',
    'EXACT': '확인됨', 'STALE': '오래된 관측값', 'MISSING': '자료 없음', 'INSUFFICIENT_COVERAGE': '자료 불완전',
    'EXIT_PROTECTION': '보호 기준가 도달', 'EXIT_THESIS_INVALID': '투자 근거 무효',
    'EXIT_TIME_LIMIT': '최대 보유 기간 도달', 'EXIT_TREND_FAILURE': '추세 유지 조건 이탈',
    'EXIT_OVERDUE': '보유 기한 초과', 'REDUCE_TO_LIMIT': '위험 한도에 맞춰 수량 축소',
    'ENTRY_ACCEPTED': '진입 조건 충족', 'KEEP_QUANTITY': '보유 수량 유지', 'UNEXECUTABLE': '현재 주문 실행 불가',
    'MONITOR_DEGRADED': '보호 감시 장애', 'MONITOR_RECOVERED': '보호 감시 복구',
    'BROKER_PAGINATION_INCOMPLETE': '증권사 계좌 조회가 끝까지 완료되지 않음',
    'NO_MARGIN_BUYING_POWER_UNVERIFIED': '미수 없는 매수 가능 금액 확인 실패',
    'NO_MARGIN_BUYING_POWER_EXCEEDED': '미수 없는 매수 가능 금액·수량 초과',
    'PROVIDER_FIELD_MISSING': '증권사 응답에 필수 항목이 없음',
    'PROVIDER_FIELD_UNVERIFIED': '증권사 응답값을 해석하지 못함',
    'MALFORMED_RESPONSE': '증권사 응답 형식이 올바르지 않음',
    'ACCOUNT_INCOMPLETE': '계좌 조회 불완전', 'ORDER_RECONCILIATION_REQUIRED': '주문·체결 대조 필요',
    'RECONCILE_REQUIRED': '계좌·체결 대조 필요', 'PRICE_UNVERIFIED': '현재 가격 미확인',
    'STALE_QUOTE': '증권사 호가 시각이 오래되어 사용 불가',
    'STREAM_QUOTE_STALE': '최근 체결 시세 없음', 'STREAM_SESSION_UNVERIFIED': '현재 시세 연결의 거래일 확인 불가',
    'STREAM_NOT_READY': '실시간 시세 첫 수신 대기', 'STREAM_CONNECTION_FAILED': '실시간 시세 연결 끊김',
    'STREAM_SESSION_INVALID': '정규장 시세 조건 불일치',
    'PROCESS_FAILED': 'Codex 실행 실패', 'MODEL_FAILED': '모델 실행 실패', 'CODEX_LOGIN_REQUIRED': 'Codex 로그인 확인 필요',
    'INPUT_INVALID': 'AI 입력 검증 실패 · 모델 호출 전 중단', 'MODEL_INPUT_INVALID': 'AI 입력 검증 실패 · 모델 호출 전 중단',
    'SNAPSHOT_VALIDATION': '모델 호출 전 입력 검사', 'INVALID_SNAPSHOT_FIELDS': 'AI 입력 항목과 허용 규격 불일치',
    'AUTHENTICATED': '로그인 확인됨', 'QUOTA_EXHAUSTED': '모델 사용 한도 소진', 'TIMEOUT': '응답 시간 초과',
    'AUTHENTICATED_AT_STARTUP': '시작 시 로그인 확인됨', 'AUTH_FAILED': '인증 확인 실패',
    'AUTH_MODE_MISMATCH': '저장된 로그인 방식과 엔진 설정 불일치', 'AUTH_STORAGE_PERMISSION': '인증 저장소 접근 권한 확인 필요',
    'AUTH_FILE_INVALID': '인증 파일을 읽을 수 없음', 'ENABLED': '사용 가능', 'NOT_RUNNING': '실행 중이 아님',
    'CURRENT': '현재 계정 조회 완료', 'UNAVAILABLE': '현재 조회 불가', 'RPC_FAILED': '사용량 조회 연결 실패',
    'NAV_NOT_FINALIZED': '마감 자산 평가 미확정', 'NAV_FINALIZED': '마감 자산 평가 확정',
    'NEED_INITIAL_AND_FINAL_NAV': '시작·종료 자산 평가가 모두 필요함', 'NONEXACT_VALUATION': '불완전한 평가 기록 포함',
    'UNALLOCATED': '전략에 귀속되지 않은 자산 존재', 'FLOW_VALUATION_MISSING': '입출금 시점 평가 누락',
    'offline': '오프라인 검증', 'paper': '모의 거래', 'shadow': '관찰', 'live': '실거래',
    'strategy_entry': '전략 진입', 'inherited': '기존 보유 종목 편입',
    'FETCH_FAILED': '조회 실패', 'TRANSIENT_FAILURE': '증권사 서버 오류',
    'TRANSPORT_FAILED': '증권사 연결 실패', 'RATE_LIMITED': '조회 요청 한도 초과',
    'NETWORK_FAILURE': '네트워크 통신 실패', 'DNS_FAILURE': '서버 주소 확인 실패',
    'TLS_FAILURE': '보안 연결 실패', 'CONNECTION_FAILURE': '연결 끊김 또는 접속 실패',
    'OUTSIDE_SESSION': '장 운영 시간 밖 · 다음 거래 시간 대기',
    'HumanRequired': '자동 처리를 완료하지 못함', 'ACCOUNT_RECOVERED': '계좌 조회 복구',
    'balance': '잔고', 'orders': '주문·체결', 'cancelable': '취소 가능한 주문', 'reservations': '예약 주문',
    'inquire-psbl-order': '미수 없는 매수 가능 금액 조회', 'inquire-balance': '잔고 조회',
    'inquire-daily-ccld': '주문·체결 조회', 'inquire-psbl-rvsecncl': '취소 가능 주문 조회',
    'order-resv-ccnl': '예약 주문 조회', 'account_normalization': '계좌 응답값 대조',
    'AUTHORIZATION_CHECK': '권한 확인', 'TOKEN_PREPARATION': '인증 토큰 준비', 'BROKER_REQUEST': '증권사 요청·응답',
    'chat': '일반 대화', 'review': '투자 판단',
    'INTERRUPTED_RECONCILE_REQUIRED': '재시작으로 중단된 이전 작업 · 계좌 자동 대조 중',
    'OWNERSHIP_RECONCILIATION_REQUIRED': '보유 자산의 전략 귀속 확인 필요',
    'ACCOUNT_CASH_RECONCILIATION_REQUIRED': '계좌 현금 대조 필요',
    'CASH_FLOW_UNCLASSIFIED': '현금 차이의 입출금·비용 구분 미완료',
    'NAV_RISK_UNVERIFIED': '현재 자산·손실 위험의 검증 미완료',
    'CASH_RECONCILIATION_FILE_INVALID': '현금 대조 근거 파일을 안전하게 읽을 수 없음',
    'CASH_RECONCILIATION_EVIDENCE_INVALID': '현금 대조 근거 파일의 형식·계좌·금액 확인 필요',
    'PORTFOLIO_VALUATION_UNAVAILABLE': '보유 수량·현금은 저장 기록이며 현재 평가를 구성하지 못함',
    'full_review': '전체 투자 검토', 'event_review': '새 공시 영향 검토',
}
INTERNAL = {'id', 'run_id', 'session_id', 'intent_id', 'broker_id', 'request_id', 'thesis_id', 'approval_id',
            'event_ids', 'fact_ids', 'source_uris', 'route', 'chat_id', 'user_id', 'requested_by', 'code_id',
            'paths', 'path', 'artifacts', 'schema_version', 'performance_index', 'subscription_quota_is_not_api_billing',
            'frames', 'input_snapshot_id', 'attempt_id', 'call_id', 'request_key', 'event_id'}


def _number(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _amount(value, unit='원'):
    number = _number(value)
    if number is None:
        return MISSING
    text = format(number, ',f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text + unit


def _time(value):
    if value is None:
        return MISSING
    try:
        stamp = (datetime.fromtimestamp(float(value), timezone.utc) if isinstance(value, (int, float))
                 else datetime.fromisoformat(str(value).replace('Z', '+00:00')))
        if stamp.tzinfo is None:
            return str(value) + ' (시간대 미확인)'
        return stamp.astimezone(ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S KST')
    except (ValueError, OverflowError, OSError):
        return str(value) + ' (시각 형식 미확인)'


def _value(value, key=''):
    if value is None or value == '':
        return MISSING
    if key == 'review_scope':
        return {'FULL': '전체 후보·보유 종목', 'PARTIAL': '해당 공시의 영향 종목만'}.get(value, str(value))
    if isinstance(value, bool):
        return '예' if value else '아니요'
    if key in {'at', 'as_of', 'resets_at', 'observed_at'} or key.endswith('_at'):
        return _time(value)
    if key.endswith('_krw') or key in {'nav', 'pnl', 'mark', 'price', 'value', 'average_entry', 'current_stop',
            'initial_stop', 'cumulative_notional', 'operating_cost', 'net_external_flow', 'pnl_after_operating_cost', 'risk_budget'}:
        return _amount(value)
    if 'quantity' in key:
        return _amount(value, '주')
    if key in {'twr', 'max_drawdown'}:
        return _amount(_number(value) * 100, '%') if _number(value) is not None else MISSING
    return STATES.get(str(value), str(value))


def _items(data):
    return ((key, value) for key, value in data.items()
            if key not in INTERNAL and not key.startswith('_') and not key.endswith(('_hash', '_path')))


def _lines(data, *, symbols=None, prefix=''):
    if isinstance(data, dict):
        result = []
        for key, value in _items(data):
            label = LABELS.get(key, key.replace('_', ' '))
            if isinstance(value, (dict, list)):
                result.append(prefix + label + ':')
                result.extend(_lines(value, symbols=symbols, prefix=prefix + '  '))
            else:
                text = _name(data, symbols) if key == 'instrument_id' else _value(value, key)
                if re.fullmatch(r'[a-fA-F0-9]{32,64}', text) or text.startswith(('/app/', '/workspace/', '/home/')):
                    text = '내부 기록에 보관'
                result.append(prefix + label + ': ' + text)
        return result
    if isinstance(data, list):
        return [line for index, row in enumerate(data, 1)
                for line in ([prefix + f'{index}.'] + _lines(row, symbols=symbols, prefix=prefix + '  '))] or [prefix + '없음']
    return [prefix + _value(data)]


def _name(row, symbols=None):
    symbol = str(row.get('instrument_id') or row.get('symbol') or '')
    name = row.get('name') or (symbols or {}).get(symbol)
    if isinstance(name, dict):
        name = name.get('name')
    code = symbol.split(':')[-1]
    return f'{name} ({code})' if name and code and name != code else str(name or code or '종목 미확인')


def reported_fee(data):
    value = data.get('fee_delta_krw')
    # Legacy zero deltas also represented missing broker fees; never infer confirmed zero.
    return value if data.get('fees_confirmed', _number(value) not in (None, 0)) else None


def diagnostic_text(data, *, symbols=None):
    """Describe the observation; an exception class is not an operator instruction."""
    rows = data.get('diagnostics') or []
    if rows:
        parts = []
        for row in rows:
            if isinstance(row, str):
                parts.append(_value(row))
                continue
            subject = _name(row, symbols) if row.get('instrument_id') else _value(row.get('endpoint', row.get('scope', '조회')))
            text = subject + ': ' + _value(row.get('detail') or row.get('reason'))
            evidence = [label + str(row[key]) for key, label in (('http_status', 'HTTP '), ('provider_code', '증권사 코드 '))
                        if row.get(key) is not None]
            if row.get('field'):
                evidence.append('응답 항목: ' + row['field'])
            if row.get('request_stage'):
                evidence.append('단계: ' + _value(row['request_stage']))
            if row.get('error_type'):
                evidence.append('예외: ' + row['error_type'])
            if row.get('transport_error'):
                evidence.append(_value(row['transport_error']))
            if row.get('provider_message'):
                evidence.append('증권사 응답: ' + row['provider_message'])
            if row.get('elapsed_seconds') is not None:
                evidence.append(str(row['elapsed_seconds']) + '초')
            parts.append(text + (' (' + ', '.join(evidence) + ')' if evidence else ''))
        return '; '.join(parts)
    if data.get('reasons'):
        return ((_name(data, symbols) + ': ') if data.get('instrument_id') else '') + ', '.join(_value(reason) for reason in data['reasons'])
    reason = data.get('reason') or data.get('action')
    prefix = 'External account observations incomplete: '
    if isinstance(reason, str) and reason.startswith(prefix):
        return '계좌 자료 미완료: ' + ', '.join(_value(code) for code in reason[len(prefix):].split(','))
    return _value(reason) if reason else '상세 원인이 기록되지 않아 확정할 수 없습니다.'


def render_notification(payload, *, symbols=None) -> str:
    """Render a flat event, {kind, payload} envelope, or command result as plain text."""
    reject_credentials(payload)
    reject_credentials(symbols)
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return '\n'.join(_lines(payload, symbols=symbols))
    data = {**payload, **payload['payload']} if isinstance(payload.get('payload'), dict) else dict(payload)
    data.pop('payload', None)
    if data.get('reply_text'):
        return str(data['reply_text'])
    kind = data.get('kind', '')
    status = data.get('status', '')
    if not isinstance(status, str):
        status = ''
    if kind in {'ACCOUNT_INCOMPLETE', 'MONITOR_DEGRADED'}:
        title = '계좌 조회 재확인 중' if kind == 'ACCOUNT_INCOMPLETE' else '보호 감시 재확인 중'
        impact = ('계좌 확인 전까지 신규 투자 판단을 보류합니다.' if kind == 'ACCOUNT_INCOMPLETE' else
                  '현재 자료로 보호 조건을 모두 확인하지 못했습니다. 신규 위험을 늘리지 않습니다.')
        if data.get('occurred_at'):
            title += '\n발생 시각: ' + _time(data['occurred_at'])
        return '\n'.join([title, '확인된 문제: ' + diagnostic_text(data, symbols=symbols), '영향: ' + impact,
                          '자동으로 다시 확인합니다. /status에서 현재 상태와 확인 시각을 볼 수 있습니다.'])
    if kind in {'ACCOUNT_RECOVERED', 'MONITOR_RECOVERED'}:
        return (('계좌 조회가 복구되었습니다. 잔고·주문 자료 대조를 마쳤습니다.' if kind == 'ACCOUNT_RECOVERED' else
                 '보호 감시가 복구되었습니다. 60초 동안 정상 확인이 이어졌습니다.') +
                ('\n확인 시각: ' + _time(data.get('checked_at') or data['occurred_at'])
                 if data.get('checked_at') or data.get('occurred_at') else ''))
    if kind == 'SERVICE_REQUEST_RECOVERED':
        return '재시작으로 중단된 이전 작업은 자동 재실행하지 않았습니다.\n계좌·주문은 감시 루프에서 자동으로 대조합니다. 현재 상태는 /status에서 확인할 수 있습니다.'
    if status == 'CANDIDATE_CONTROLS':
        controls = data['controls']
        return '\n'.join(['종목 제외 설정 · 조회만 수행했습니다.',
            '투자 후보 심사에서 제외: ' + (', '.join(_name({'instrument_id': symbol}, symbols) for symbol in controls['removed']) or '없음'),
            '매수 금지: ' + (', '.join(_name({'instrument_id': symbol}, symbols) for symbol in controls['excluded']) or '없음'),
            '이 화면은 실제 투자 후보 목록이 아닙니다. 후보 선정 결과와 근거는 /report에서 확인하세요.'])
    if kind in {'ORDER_ACKNOWLEDGED', 'CUMULATIVE_FILL', 'FILL_CORRECTION'}:
        title = {'ORDER_ACKNOWLEDGED': '주문 접수', 'CUMULATIVE_FILL': '체결 확인', 'FILL_CORRECTION': '체결 기록 정정'}[kind]
        lines = [f'{title} · {_name(data, symbols)} · {_value(data.get("side"))}']
        fields = (('quantity',) if kind == 'ORDER_ACKNOWLEDGED' else
                  ('quantity_delta', 'notional_delta_krw', 'fee_delta_krw', 'cumulative_quantity'))
        for key in fields:
            if key in data:
                value = reported_fee(data) if key == 'fee_delta_krw' else data[key]
                lines.append(f'{LABELS[key]}: {_value(value, key)}')
        lines.append('이유: ' + _value(data.get('reason')))
        for key in ('observed_at', 'created_at'):
            if key in data:
                lines.append('시각: ' + _time(data[key]))
                break
        if kind == 'ORDER_ACKNOWLEDGED':
            lines.append('주문 접수이며, 실제 체결은 별도로 확인합니다.')
        return '\n'.join(lines)
    if status == 'REPORT_READY':
        report = data.get('report', {})
        return f'{report.get("date", "오늘")} 일일 리포트를 생성했습니다.\n첨부 HTML에서 거래·판단·보호 감시 기록을 확인하세요.'
    if 'mode' in data and 'holdings' in data:
        lines = ['엔진 상태', '운영: ' + _value(data['mode'])]
        for key in ('authentication', 'model_status', 'account_status', 'monitor_status', 'review_status', 'scheduler_status'):
            if key in data:
                lines.append(LABELS[key] + ': ' + _value(data[key]))
        if data.get('model_id'):
            lines.append('사용 모델: ' + data['model_id'])
        for key in ('chat_model', 'review_model'):
            health = data.get(key, {})
            if key in data:
                lines.append(LABELS[key] + ': ' + _value(health.get('status', 'NOT_CALLED')) +
                             (' · ' + _time(health['checked_at']) if health.get('checked_at') else ''))
        if data.get('model_checked_at'):
            lines.append('최근 모델 실행: ' + _time(data['model_checked_at']) + ' · ' + _value(data.get('model_purpose')))
        for key in ('account_checked_at', 'account_succeeded_at', 'monitor_checked_at'):
            if data.get(key):
                lines.append(LABELS[key] + ': ' + _time(data[key]))
        if data.get('account_diagnostics'):
            lines.append('현재 계좌 문제: ' + diagnostic_text({'diagnostics': data['account_diagnostics']}))
        if data.get('monitor_status') == 'MONITOR_DEGRADED':
            lines.append('현재 감시 문제: ' + diagnostic_text(data.get('monitor_diagnostic') or {}, symbols=symbols))
        if data.get('blocked_reasons'):
            lines.append('신규 투자 차단 이유: ' + ', '.join(_value(reason) for reason in data['blocked_reasons']))
        if data.get('cash_reconciliation'):
            cash = data['cash_reconciliation']
            lines.append('미분류 현금 차이: ' + str(cash['pending_count']) + '건')
            if cash.get('reason'):
                lines.append('현금 대조 문제: ' + _value(cash['reason']))
        diagnostic = data.get('model_diagnostic') or {}
        if data.get('model_status') not in {'SUCCESS', 'NOT_CALLED', None} and diagnostic:
            if diagnostic.get('stage'):
                lines.append('모델 진단: ' + _value(diagnostic['stage']) + ' / ' + _value(diagnostic.get('reason')))
            else:
                lines.append('모델 진단: ' + _value(diagnostic.get('category')) + ' / 종료 코드 ' + str(diagnostic.get('exit_code', '미확인')))
        lines += ['전략 현금: ' + _amount(data.get('cash_krw')), f"보유 {len(data['holdings'])}종목 · 진행 중 주문 {len(data.get('working_orders', []))}건"]
        for row in data['holdings']:
            lines.append('• ' + _name(row, symbols) + ' ' + _amount(row.get('quantity'), '주'))
        for key in ('as_of', 'version', 'reasoning_effort'):
            if key in data:
                lines.append(LABELS.get(key, key) + ': ' + _value(data[key], key))
        lines.append('대화: ' + ('이어지는 대화 있음 · /new로 초기화' if data.get('session_active') else '새 대화 준비'))
        lines.append('운영 변경은 아래 버튼에서 선택하세요. /stop은 현재 대화 응답만 중단합니다.')
        lines.append('표시된 시각의 저장 상태입니다. 이 명령은 계좌 재조회·복구를 실행하지 않습니다.')
        return '\n'.join(lines)
    simple = {
        'NEW_SESSION': '새 대화를 시작했습니다. 거래·보유 상태는 유지됩니다.',
        'SESSION': '현재 대화를 이어갑니다. 새 대화는 /new 로 시작하세요.',
        'CANCELLED': '진행 중인 대화 응답을 취소했습니다.', 'CHAT_CANCELLED': '진행 중인 대화 응답을 취소했습니다.',
        'NO_ACTIVE_CHAT': '취소할 대화 응답이 없습니다.', 'CANCEL_REQUESTED': '대화 응답 취소를 요청했습니다.',
        'SESSION_CHANGED': '대화가 새로 시작되어 이전 응답을 종료했습니다.',
        'PAUSED': '신규 투자 판단을 일시정지했습니다.', 'RESUMED': '신규 투자 판단을 재개했습니다.',
        'SCHEDULE_ON': '예약 투자 검토를 켰습니다.', 'SCHEDULE_OFF': '예약 투자 검토를 껐습니다.',
    }
    if status in simple:
        return simple[status] + ('\n보호 감시·매도는 계속 작동합니다.' if data.get('protection') == 'CONTINUES' else '')
    if status == 'APPROVAL_REQUIRED' and 'requested_effort' in data:
        return (f'추론 수준 변경은 아직 적용되지 않았습니다.\n현재: {data.get("current_effort", MISSING)}'
                f' → 요청: {data["requested_effort"]}\n모델 지원 여부와 운영 설정 승인을 확인해야 합니다.')
    if status == 'CANDIDATE_LIST_UPDATED':
        return f'후보 종목 설정 변경 · {_name(data, symbols)}\n' + '\n'.join(_lines(data.get('controls', {}), symbols=symbols)) + '\n보유 수량 변경이나 자동 청산은 실행하지 않았습니다.'
    if 'rate_limits' in data or 'limits' in data or status in {'USAGE', 'RECORDED_ATTEMPTS'}:
        return _usage_text(data)
    if 'run_status' in data:
        lines = ['투자 검토 결과']
        for key in ('kind', 'review_scope', 'run_status', 'reason', 'model_status', 'decision_status', 'order_status'):
            if key in data:
                lines.append(LABELS[key] + ': ' + _value(data[key], key))
        if (data.get('trigger') or {}).get('instrument_id'):
            lines.append('계기 공시 종목: ' + _name(data['trigger'], symbols))
        for row in data.get('review_details', []):
            lines.append('• ' + _name(row, symbols) + ' — ' + _review_outcome(row))
        if data.get('feature_exclusions'):
            lines.append('사전 검사: ' + ' · '.join(name + ' ' + str(len(rows)) + '건'
                for name,rows in _screening_groups(data['feature_exclusions']).items() if rows) + '. 상세는 첨부 HTML에 있습니다.')
        if data.get('review_details'):
            lines.append('공시 원문·지표·시세 기준 시각과 상세 판단은 첨부 HTML에 있습니다.')
        if data.get('diagnostics'):
            lines.append('상세 원인: ' + diagnostic_text(data, symbols=symbols))
        lines.append('보호 매도 등 당일 전체 거래는 /report 에 포함됩니다.')
        return '\n'.join(lines)
    titles = {'MONITOR_DEGRADED': '보호 감시에 문제가 생겼습니다.', 'MONITOR_RECOVERED': '보호 감시가 복구되었습니다.',
              'ACCOUNT_INCOMPLETE': '계좌 조회가 완료되지 않았습니다.', 'DISCRETIONARY_PAUSED': '신규 투자 판단이 일시정지되었습니다.',
              'DRAWDOWN_PAUSED': '낙폭 한도에 도달해 신규 투자를 일시정지했습니다.',
              'SERVICE_WORKER_FAILED': '서비스 작업 중단 기록입니다.', 'SERVICE_REQUEST_RECOVERED': '재시작 후 이전 작업 상태를 확인했습니다.',
              'NOTIFY_BLOCKED': '알림 전송이 중단되었습니다.'}
    title = titles.get(kind, '운영 상태' if 'mode' in data and 'holdings' in data else '처리 결과')
    lines = _lines({key: value for key, value in data.items() if key != 'kind'}, symbols=symbols)
    if kind == 'ACCOUNT_INCOMPLETE':
        lines.append('완전한 계좌 자료가 확인되기 전까지 신규 투자 판단을 진행할 수 없습니다.')
    return title + ('\n' + '\n'.join(lines) if lines else '\n상세 자료가 없습니다.')


def _usage_text(data):
    limits = data.get('rate_limits', data.get('limits', {}))
    limits = limits.values() if isinstance(limits, dict) else limits
    lines = ['Codex 사용량']
    for window in limits or []:
        if not isinstance(window, dict):
            continue
        if 'primary' in window or 'secondary' in window:
            lines.extend(_usage_text({'limits': list(window.values())}).splitlines()[1:])
            continue
        if not any(key in window for key in ('used_percent', 'usedPercent', 'window_minutes', 'windowDurationMins')):
            continue
        used = _number(window.get('used_percent', window.get('usedPercent')))
        minutes = window.get('window_minutes', window.get('windowDurationMins'))
        label = '5시간' if minutes == 300 else '주간' if minutes == 10080 else f'{minutes}분' if minutes else '한도'
        remaining = _amount(max(Decimal(0), min(Decimal(100), 100 - used)), '%') if used is not None else MISSING
        lines.append(f'{label} 남은 한도: {remaining}')
        reset = window.get('resets_at', window.get('resetsAt'))
        if reset is not None:
            lines.append('  초기화: ' + _time(reset))
    if len(lines) == 1:
        lines.append('구독 잔여 한도: 조회되지 않았습니다.')
    if data.get('reason'):
        lines.append('조회 상태: ' + _value(data['reason']))
    if 'attempts' in data:
        attempts = data['attempts']
        if isinstance(attempts, list):
            calls = sum(row.get('record_type') == 'MODEL_ATTEMPT' for row in attempts)
            outcomes = sum(row.get('record_type') == 'MODEL_OUTCOME' for row in attempts)
            lines.append(f'최근 기록: 호출 시도 {calls}회 · 결과 {outcomes}건 (최대 100개 기록)')
        else:
            lines.append(_value(attempts))
        if isinstance(attempts, list) and attempts:
            lines.append('최근 결과: ' + _value(attempts[0].get('status')))
            usage = attempts[0].get('usage')
            if isinstance(usage, dict):
                lines.append('최근 시도 토큰: 입력 ' + str(usage.get('input_tokens', '미제공')) +
                             ' · 출력 ' + str(usage.get('output_tokens', '미제공')))
    if data.get('usage'):
        lines.extend(_lines(data['usage']))
    lines.append('구독 한도와 API 과금·운영 비용은 별도입니다.')
    return '\n'.join(lines)


def _table(headers, rows, *, empty='기록이 없습니다.'):
    if not rows:
        return '<p class="muted">' + html.escape(empty) + '</p>'
    return ('<table class="report-table"><thead><tr>' + ''.join(f'<th scope="col">{html.escape(h)}</th>' for h in headers)
            + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td data-label="{html.escape(label, quote=True)}">{html.escape(str(cell))}</td>' for label, cell in zip(headers, row))
                                               + '</tr>' for row in rows) + '</tbody></table>')


def _details(title, data, symbols=None):
    return '<details><summary>' + html.escape(title) + '</summary><pre>' + html.escape('\n'.join(_lines(data, symbols=symbols))) + '</pre></details>'


def _nav_chart(points):
    valid = []
    for point in points:
        value = _number(point.get('nav'))
        try:
            at = datetime.fromisoformat(str(point.get('at')).replace('Z', '+00:00'))
            if value is not None and at.tzinfo is not None:
                valid.append((at, value, point))
        except ValueError:
            continue
    valid.sort(key=lambda item: item[0])
    if len(valid) < 2:
        return '<p class="muted">자산 그래프를 그릴 평가 기록이 부족합니다. 시작·종료 시점 기록이 필요합니다.</p>'
    low, high = min(p[1] for p in valid), max(p[1] for p in valid)
    span = (valid[-1][0] - valid[0][0]).total_seconds() or 1
    coordinates = []
    for at, value, _ in valid:
        x = 100 + 670 * (at - valid[0][0]).total_seconds() / span
        y = 150 if high == low else 240 - float((value - low) / (high - low)) * 190
        coordinates.append(f'{x:.1f},{y:.1f}')
    svg = ('<svg class="chart" viewBox="0 0 800 300" role="img" aria-label="기록된 전략 자산 평가 추이">'
           '<title>전략 자산 평가 추이. 입출금 조정 수익률과 다릅니다.</title>'
           '<path d="M100 35V245H770" fill="none" stroke="#cbd7e2"/>'
           f'<polyline points="{" ".join(coordinates)}" fill="none" stroke="#087d86" stroke-width="3"/>'
           f'<text x="94" y="48" text-anchor="end">{html.escape(_amount(high))}</text>'
           f'<text x="94" y="244" text-anchor="end">{html.escape(_amount(low))}</text>'
           f'<text x="100" y="278">{valid[0][0].astimezone(ZoneInfo("Asia/Seoul")).strftime("%m/%d %H:%M")}</text>'
           f'<text x="770" y="278" text-anchor="end">{valid[-1][0].astimezone(ZoneInfo("Asia/Seoul")).strftime("%m/%d %H:%M")} KST</text></svg>')
    uncertain = sum(point.get('quality') != 'EXACT' for _, _, point in valid)
    return svg + f'<p>기록 범위: {html.escape(_amount(low))} ~ {html.escape(_amount(high))}</p>' + f'<p class="muted">제공된 평가 기록 {len(valid)}개 · 품질 미확인/불완전 {uncertain}개. 자산 증감에는 입출금이 포함될 수 있으며 수익률을 뜻하지 않습니다.</p>'


def _review_outcome(row):
    ai, plan = row.get('ai') or {}, row.get('plan') or {}
    if ai:
        text = _value(ai.get('verdict', ai.get('action'))) + ': ' + str(ai.get('reason') or ai.get('economic_path') or MISSING)
        if row.get('stage') == 'AI_PROPOSED':
            text = 'AI 응답 (실행 검증 미완료) · ' + text
    else:
        text = _value(row.get('stage'))
        if row.get('filter_reasons'):
            text += ': ' + ', '.join(_value(reason) for reason in row['filter_reasons'])
    if row.get('protection'):
        text += ' / 보호 판단: ' + _value(row['protection'].get('action'))
        if row['protection'].get('reasons'):
            text += ' · ' + ', '.join(_value(reason) for reason in row['protection']['reasons'])
    if plan:
        text += ' / 주문 계획: ' + _value(plan.get('reason')) + ' · ' + _amount(plan.get('quantity'), '주')
    elif not row.get('orders'):
        text += ' / 이 검토의 주문 없음'
    if row.get('orders'):
        text += ' / ' + ', '.join(_value(order.get('state')) + ' ' + _amount(order.get('quantity'), '주') for order in row['orders'])
    return text


def _screening_groups(rows):
    groups = {'자료 확인 실패':[], '시세 수신 대기':[], '전략 조건 제외':[], '수집 대상 아님':[]}
    for row in rows:
        reason = row.get('reason')
        group = ('수집 대상 아님' if reason in {'NO_RECENT_EVENT_TO_REVIEW','UNIVERSE_STATUS_OR_CLASSIFICATION_EXCLUDED'} else
                 '시세 수신 대기' if reason == 'STREAM_NOT_READY' else
                 '전략 조건 제외' if reason in {'NO_VALID_RECENT_OFFICIAL_EVENT','INSUFFICIENT_LIQUIDITY','TREND_GATE_FAILED','RELATIVE_STRENGTH_GATE_FAILED','BOARD_INDEX_GATE_FAILED'} else
                 '자료 확인 실패')
        groups[group].append(row)
    return groups


def _review_html(run, symbols):
    body = ''
    for row in run.get('review_details', []):
        body += '<article class="thesis"><h3>' + html.escape(_name(row, symbols)) + '</h3>'
        body += '<p>' + html.escape(('기존 보유' if row.get('scope') == 'HOLDING' else '신규 후보') + ' · ' + _review_outcome(row)) + '</p>'
        body += '<p class="metadata">검사 시각: ' + html.escape(_time(row.get('evaluated_at'))) + '</p>'
        if row.get('stage') == 'OUTSIDE_REVIEW_SCOPE':
            body += '<p>이 종목은 공시 영향 검토 대상이 아니므로 AI 판단을 요청하지 않았습니다. 보호 감시 결과와 보유 현황을 참고로 표시합니다.</p></article>'
            continue
        features, quote = row.get('features') or {}, row.get('quote') or {}
        criteria = run.get('entry_criteria', {})
        universe, signal, order_policy = (criteria.get(key, {}) for key in ('universe', 'signal', 'orders'))
        measurements = [
            ['시세 경과 시간', _amount(row.get('quote_age_seconds'), '초'), _amount(order_policy.get('quote_max_age_seconds'), '초') + ' 이내'],
            ['호가 관측 / 수신', _time(quote.get('observed_at')) + ' / ' + _time(quote.get('received_at')), str(quote.get('source') or MISSING)],
            ['매수 / 매도 호가', _amount(quote.get('bid')) + ' / ' + _amount(quote.get('ask')), '호가 차이 ' + _amount(universe.get('maximum_spread_bps'), 'bp') + ' 이하'],
            ['완성 일봉', _amount(features.get('bars'), '개'), _amount(universe.get('minimum_completed_bars'), '개') + ' 이상'],
            ['20일 거래대금 중앙값', _amount(features.get('adtv20')), _amount(universe.get('minimum_adtv_krw')) + ' 이상'],
            ['종가 / 60일선', _amount(features.get('close')) + ' / ' + _amount(features.get('sma60')), '종가 > 60일선'],
            ['20일선 / 5거래일 전', _amount(features.get('sma20')) + ' / ' + _amount(features.get('sma20_five_sessions_ago')), '현재 20일선 ≥ 5거래일 전'],
            ['시장 대비 20일 수익률', _value(features.get('rs20')), '> ' + str(signal.get('relative_strength_min_exclusive', MISSING))],
            ['시장 지수 / 60일선', _value(features.get('index_close')) + ' / ' + _value(features.get('index_sma60')), '지수 ≥ 60일선'],
            ['가격 변동폭 ATR', _amount(features.get('atr14')), '매수할 매도호가 ≤ 전일 종가 + ' + str(signal.get('chase_above_previous_close_atr', MISSING)) + 'ATR, 20일선 + ' + str(signal.get('chase_above_sma20_atr', MISSING)) + 'ATR'],
        ]
        body += '<p>지표 기준: ' + html.escape(_time(features.get('as_of'))) + ' · 마지막 일봉: ' + html.escape(str(features.get('last_session_id', MISSING))) + '</p>'
        body += _table(['검사 항목', '관측값', '기준'], measurements)
        order_quote = row.get('order_quote')
        if order_quote:
            current = order_quote.get('quote') or {}
            body += '<h4>주문 계획 직전 시세</h4>' + _table(['검사 시각','매수 / 매도 호가','관측 / 수신 시각','경과 시간'], [[
                _time(order_quote.get('checked_at')), _amount(current.get('bid')) + ' / ' + _amount(current.get('ask')),
                _time(current.get('observed_at')) + ' / ' + _time(current.get('received_at')), _amount(order_quote.get('quote_age_seconds'),'초')]])
        if row.get('filter_reasons'):
            body += '<p>사전 검사: ' + html.escape(', '.join(_value(reason) for reason in row['filter_reasons'])) + '</p>'
        body += '<h4>공식 공시 근거</h4>'
        for event in row.get('evidence', []):
            body += '<p>' + html.escape(str(event.get('family', '공시')) + ' · 이용 가능 시각: ' + _time(event.get('available_at'))) + '</p>'
            uri = event.get('source_uri', '')
            if isinstance(uri, str) and uri.startswith(('https://', 'http://')):
                body += '<p><a href="' + html.escape(uri, quote=True) + '">공식 공시 원문</a></p>'
            body += _table(['추출 사실', '값'], [[str(key), str(value)] for key, value in event.get('facts', {}).items()])
            body += '<p>비교 근거: ' + html.escape(str(event.get('comparison_basis') or MISSING)) + '</p>'
        if not row.get('evidence'):
            body += '<p>이 검토에 제공된 공식 공시 근거가 없습니다.</p>'
        if row.get('facts'):
            body += _table(['근거 참조', '관측 사실', '단위', '출처'], [[str(fact.get('fact_id','')),
                str(fact.get('value',MISSING)),str(fact.get('unit','')),str(fact.get('source',''))] for fact in row['facts']])
        ai = row.get('ai') or {}
        body += _table(['AI 판단 근거', '내용'], [[label, _value(ai[key])] for key, label in (
            ('priority', '검토 우선순위'), ('economic_path', '투자 근거'), ('horizon_case', '예상 기간'),
            ('priced_in_case', '가격 반영 여부'), ('counterevidence_fact_ids', '반대 근거 참조'),
            ('invalidation_case', '판단 무효 조건'), ('uncertainties', '불확실성'), ('reason', '보유 판단 이유')) if key in ai],
            empty='AI 판단이 수행되지 않았거나 완료되지 않았습니다.')
        body += '</article>'
    return body


def _operational_report(data):
    daily = isinstance(data.get('status'), dict)
    status = data['status'] if daily else data.get('portfolio', {})
    symbols = data.get('instruments', {})
    runs = data.get('runs', []) if daily else [data]
    orders, fills, nav = data.get('orders', []), data.get('fills', []), data.get('nav', [])
    run_trades = not daily and data.get('trade_scope') == 'RUN'
    holdings = status.get('holdings', [])
    body = f'<p class="metadata">대상일: {html.escape(str(data.get("date", "실행 결과")))} · 생성: {html.escape(_time(data.get("created_at")))}</p>'
    sections = [('overview', '요약'), ('holdings', '보유 종목'), ('decisions', '판단·근거'), ('trades', '주문·체결'), ('ledger', '자산 기록'), ('diagnostics', '운영 진단')]
    body += '<nav class="report-nav" aria-label="리포트 바로가기">' + ''.join(f'<a href="#{key}">{name}</a>' for key, name in sections) + '</nav>'
    body += '<section id="overview"><h2>핵심 요약</h2><div class="cards">'
    cards = [('운영 모드', _value(status.get('mode', data.get('mode')))), ('보유 종목', f'{len(holdings)}개' if 'holdings' in status else MISSING),
             ('이번 검토 주문 기록' if run_trades else '주문 기록', f'{len(orders)}건' if 'orders' in data else MISSING),
             ('이번 검토 체결·정정 기록' if run_trades else '체결·정정 기록', f'{len(fills)}건' if 'fills' in data else MISSING),
             ('전략 현금', _amount(status.get('cash_krw', status.get('allocated_cash'))))]
    body += ''.join(f'<div class="card">{html.escape(label)}<strong>{html.escape(value)}</strong></div>' for label, value in cards) + '</div>'
    body += '<p class="notice">이 보고서는 저장된 관측·판단·체결 기록입니다. 주문 접수와 체결을 구분하며, 누락된 자료를 0원이나 거래 없음으로 간주하지 않습니다. 실제 투자 성과는 별도 검증이 필요합니다.</p>'
    if not daily:
        body += _table(['항목', '결과'], [[LABELS[key], _value(data[key], key)] for key in ('kind', 'review_scope', 'run_status', 'reason', 'model_status', 'decision_status', 'order_status') if key in data])
        if data.get('trigger'):
            body += _details('계기 공시', data['trigger'], symbols)
        if data.get('review_targets') is not None:
            targets = data['review_targets']
            body += '<p>확정된 AI 검토 대상: 신규 후보 ' + str(len(targets['candidate_ids'])) + '개 · 보유 종목 ' + str(len(targets['position_ids'])) + '개.</p>'
        stages = {'input.snapshot.json': '판단 입력 확정', 'candidates.json': '후보 사전 검사', 'proposal.json': 'AI 응답',
                  'decision.json': 'AI 응답 검증', 'plan.json': '주문·보호 계획', 'execution.json': '주문 실행'}
        if data.get('unreached_stages'):
            body += '<p>생성되지 않은 단계 기록: ' + html.escape(', '.join(stages.get(name, name) for name in data['unreached_stages'])) + '.</p>'
        body += _details('저장된 계좌·평가 확인 상태', {key: status[key] for key in
            ('as_of', 'reconciled', 'complete', 'account_checked_at', 'account_succeeded_at') if key in status})
        if data.get('portfolio_diagnostic'):
            body += _details('보유 평가 문제', data['portfolio_diagnostic'])
    else:
        health_keys = ('model_id', 'authentication', 'review_status', 'account_status', 'account_checked_at', 'account_succeeded_at', 'monitor_status', 'monitor_checked_at')
        body += _table(['현재 상태', '확인 결과'], [[LABELS[key], _value(status[key], key)] for key in health_keys if key in status])
        for key in ('chat_model', 'review_model'):
            health = status.get(key, {})
            if key in status:
                body += '<p>' + LABELS[key] + ': ' + html.escape(_value(health.get('status', 'NOT_CALLED'))) + ' · ' + html.escape(_time(health.get('checked_at'))) + '</p>'
        if status.get('account_diagnostics'):
            body += '<p class="notice">' + html.escape(diagnostic_text({'diagnostics': status['account_diagnostics']})) + '</p>'
        if status.get('monitor_status') == 'MONITOR_DEGRADED':
            body += '<p class="notice">현재 보호 감시 문제: ' + html.escape(diagnostic_text(status.get('monitor_diagnostic') or {}, symbols=symbols)) + '</p>'
        body += '<p class="muted">일반 대화 성공은 투자 판단 완료를 뜻하지 않습니다. 상태별 확인 시각과 아래 과거 사건을 구분해 보세요.</p>'
    if any(key in status for key in ('nav_risk_unverified', 'cash_reconciliation', 'new_risk_allowed')):
        body += _details('신규 투자 가능 여부와 현금 대조', {key: status[key] for key in
            ('new_risk_allowed', 'blocked_reasons', 'nav_risk_unverified', 'performance_uncertain', 'cash_reconciliation') if key in status})
    body += '</section><section id="holdings"><h2>보유 종목</h2>'
    holding_rows = []
    for row in holdings:
        value = row.get('value', row.get('value_krw'))
        price, quantity = _number(row.get('price', row.get('mark'))), _number(row.get('quantity'))
        if value is None and row.get('valuation_quality') == 'EXACT' and price is not None and quantity is not None:
            value = price * quantity
        holding_rows.append([_name(row, symbols), _amount(row.get('quantity'), '주'), _amount(price),
            _amount(value), _value(row.get('valuation_quality')), _time(row.get('price_observed_at', row.get('valuation_at')))])
    body += _table(['종목', '보유 수량', '평가 단가', '평가 금액', '평가 품질', '가격 기준 시각'], holding_rows,
        empty='보유 종목이 없습니다.' if 'holdings' in status else '보유 종목 자료가 제공되지 않았습니다.')
    body += '</section><section id="decisions"><h2>판단·근거</h2>'
    body += _table(['시각', '실행', '모델', '판단', '이번 검토의 주문', '이유'],
        [[_time(run.get('created_at')), _value(run.get('run_status')), _value(run.get('model_status')),
          _value(run.get('decision_status')), _value(run.get('order_status')), _value(run.get('reason'))] for run in runs],
        empty='기록된 투자 검토가 없습니다. 보호 매도 여부는 아래 주문·체결 장부에서 별도로 확인합니다.')
    body += '<p class="muted">위 주문 결과는 해당 투자 검토의 범위입니다. ' + (
        '아래 장부도 이번 검토에 연결된 주문·체결만 표시합니다. 보호 감시를 포함한 당일 전체 거래는 /report 에서 확인합니다.' if run_trades else
        '보호 규칙으로 발생한 매도를 포함한 전체 거래는 주문·체결 장부에 표시합니다.') + '</p>'
    for run in runs:
        if run.get('feature_exclusions'):
            for group,rows in _screening_groups(run['feature_exclusions']).items():
                if rows:
                    body += '<details><summary>' + group + ' (' + str(len(rows)) + '건)</summary>'
                    body += _table(['종목 또는 출처','이유'], [[_name(row,symbols) if row.get('instrument_id') else str(row.get('source','자료 수집')),
                        ', '.join(_value(reason) for reason in row.get('reasons') or [row.get('reason')])] for row in rows]) + '</details>'
        if run.get('review_details'):
            body += '<h3>' + html.escape(_time(run.get('created_at'))) + ' 종목별 검토</h3>' + _review_html(run, symbols)
    for thesis in data.get('theses', []):
        body += '<article class="thesis"><h3>' + html.escape(_name(thesis, symbols)) + '</h3><dl>'
        for key in ('economic_path', 'horizon_case', 'counterevidence', 'invalidation_case', 'current_stop', 'max_holding_sessions', 'origin', 'exit_reason'):
            body += '<dt>' + LABELS[key] + '</dt><dd>' + html.escape(_value(thesis.get(key), key)) + '</dd>'
        body += '</dl></article>'
    if not data.get('theses'):
        body += '<p class="muted">종목별 투자 근거·보호 조건이 제공되지 않았습니다.</p>'
    body += '</section><section id="trades"><h2>' + ('이번 검토 주문·체결 기록' if run_trades else '주문·체결 장부') + '</h2><h3>주문</h3>'
    body += _table(['시각', '종목', '방향', '주문 수량', '상태', '누적 체결', '누적 체결 금액', '이유'],
        [[_time(row.get('created_at')), _name(row, symbols), _value(row.get('side')), _amount(row.get('quantity'), '주'),
          _value(row.get('state')), _amount(row.get('cumulative_quantity'), '주'), _amount(row.get('cumulative_notional')), _value(row.get('reason'))] for row in orders],
        empty='기록된 주문이 없습니다.' if 'orders' in data else '전체 주문 장부가 제공되지 않았습니다.')
    body += '<h3>체결·정정</h3>' + _table(['관측 시각', '종목', '방향', '구분', '수량', '금액', '수수료·세금', '이유'],
        [[_time(row.get('at', row.get('observed_at'))), _name(row, symbols), _value(row.get('side')), '정정' if row.get('correction') else '체결',
          _amount(row.get('quantity', row.get('quantity_delta')), '주'), _amount(row.get('amount_krw', row.get('notional_delta_krw'))),
          _amount(row.get('fee_krw', row.get('fee_delta_krw'))), _value(row.get('reason'))] for row in fills],
        empty='기록된 체결이 없습니다.' if 'fills' in data else '체결 장부가 제공되지 않았습니다.')
    body += '</section><section id="ledger"><h2>자산·성과 기록</h2>' + _nav_chart(nav)
    performance = status.get('performance') or data.get('performance') or {}
    body += _details('누적 성과와 자료 완결성', performance or {'coverage': None, 'pnl': None, 'twr': None})
    body += '<p class="muted">누적 성과의 기간은 저장된 평가 기록 전체입니다. 운영 비용이 미확인이면 비용 차감 후 손익도 확정하지 않습니다.</p>'
    body += _details('자산 평가 원장', nav)
    body += '</section><section id="diagnostics"><h2>운영 진단</h2>'
    diagnostics = data.get('diagnostics', [])
    counts = {kind: sum(row.get('kind') == kind for row in diagnostics) for kind in dict.fromkeys(row.get('kind') for row in diagnostics)}
    body += _table(['사건 종류', '기록 수'], [[_value(kind), str(count)] for kind, count in counts.items()], empty='기록된 운영 장애가 없습니다.')
    body += '<p class="muted">원장에 남은 사건 수입니다. 중복 억제된 알림도 포함하며, 현재 장애 수나 Telegram 메시지 수를 뜻하지 않습니다. 최근 10건을 표시합니다.</p>'
    body += _table(['시각', '종류', '설명'], [[_time(row.get('at', row.get('created_at'))), _value(row.get('kind')),
        render_notification(row, symbols=symbols) if row.get('kind') in {'ACCOUNT_INCOMPLETE', 'ACCOUNT_RECOVERED', 'MONITOR_DEGRADED', 'MONITOR_RECOVERED'} else '\n'.join(_lines(row, symbols=symbols))]
        for row in diagnostics[-10:]], empty='별도로 제공된 진단 기록이 없습니다.')
    if len(diagnostics) > 10:
        body += _details('전체 사건 기록 (' + str(len(diagnostics)) + '건)', diagnostics, symbols)
    if not daily:
        # Preserve the complete run contract for audit readers and existing exports.
        body += '<details><summary>전체 실행 기록</summary>' + facts_html({key: data[key] for key in ('run_status', 'reason', 'decision_status', 'order_status', 'performance_status') if key in data}) + facts_html(data) + '</details>'
    body += '</section>'
    return body


def write_report(data: dict, json_path, html_path, title: str = "실행·성과 보고") -> dict:
    """Both files consume this one JSON-serializable numeric object."""
    payload = json.dumps(data, ensure_ascii=False, indent=2, default=json_default, allow_nan=False)
    normalized = json.loads(payload)
    reject_credentials(normalized)
    reject_credentials(title)
    json_path, html_path = Path(json_path), Path(html_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(payload + '\n', encoding='utf-8')
    body = f'<h1>{html.escape(title)}</h1>'
    if normalized.get('evidence_status') == 'FIXTURE_ONLY' or normalized.get('provenance') == 'FIXTURE_ONLY':
        body += '<p class="notice">합성 fixture 검증 전용입니다. 실제 투자 성과가 아닙니다. STRATEGY_UNPROVEN</p>'
    if 'run_status' in normalized or (isinstance(normalized.get('status'), dict) and 'runs' in normalized):
        body += _operational_report(normalized)
    else:
        body += facts_html(normalized)
    html_path.write_text(_document(title, body), encoding='utf-8')
    return {"json": str(json_path), "html": str(html_path)}


def render_readme(source, output, *, generated_at: datetime | None = None) -> dict:
    from markdown_it import MarkdownIt

    source, output = Path(source), Path(output)
    original = source.read_bytes()
    reject_credentials(original)
    digest = hashlib.sha256(original).hexdigest()
    created = generated_at or datetime.now(timezone.utc)
    if created.tzinfo is None:
        raise ValueError("generated_at must be timezone aware")
    renderer = MarkdownIt('commonmark', {'html': False, 'linkify': False}).enable('table')
    tokens = renderer.parse(original.decode('utf-8'))
    headings = []
    for index, token in enumerate(tokens):
        if token.type == 'heading_open':
            anchor = f'section-{len(headings) + 1}'
            token.attrSet('id', anchor)
            headings.append((token.tag[1:], anchor, tokens[index + 1].content))
    toc = f'<nav aria-label="절별 바로가기"><details><summary>전체 목차 ({len(headings)}개 절)</summary><ul>'
    toc += ''.join(f'<li class="level-{level}"><a href="#{anchor}">{html.escape(label)}</a></li>'
                   for level, anchor, label in headings)
    toc += '</ul></details></nav>'
    local = created.astimezone(ZoneInfo('Asia/Seoul')).isoformat()
    metadata = (f'<p class="metadata">원본: {html.escape(source.name)}<br>'
                f'SHA-256: <code>{digest}</code><br>생성시각: {html.escape(local)}</p>')
    notice = ('<p class="notice">구현 명세를 렌더링한 보고서입니다. 연구 가설·설정 예시는 '
              '실제 투자 성과, 검증 완료 또는 실거래 승인이 아닙니다.</p>')
    body = metadata + notice + toc + renderer.renderer.render(tokens, renderer.options, {})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_document('trading-engine 투자 전략과 거래 시스템 명세', body), encoding='utf-8')
    return {"source": str(source), "output": str(output), "source_sha256": digest,
            "created_at": created.astimezone(timezone.utc).isoformat(), "heading_count": len(headings)}
