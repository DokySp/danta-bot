"""Deterministic JSON/HTML views of the same facts; no reporting model."""

import hashlib
import html
import json
from datetime import datetime, timezone
from decimal import Decimal
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
@media(max-width:650px){body{padding:10px}main{padding:16px}h1{font-size:1.6em}th,td{padding:7px}}
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
    if 'run_status' in normalized:
        body += facts_html({key: normalized[key] for key in ('run_status', 'reason', 'decision_status', 'order_status', 'performance_status') if key in normalized})
        body += '<details><summary>전체 실행 기록</summary>' + facts_html(normalized) + '</details>'
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
