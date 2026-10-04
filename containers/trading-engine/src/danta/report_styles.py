"""Self-contained presentation for Telegram's operator HTML attachments."""

PAGES = (
    ('overview', '한눈에 보기'), ('holdings', '보유 자산'), ('decisions', '투자 판단'),
    ('trades', '주문·체결'), ('ledger', '자산·성과'), ('diagnostics', '운영 상태'),
)

OPERATOR_CSS = """
:root{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#203331;background:#f3f5f2;line-height:1.65}
*{box-sizing:border-box}
body.operator-report{max-width:1240px;padding:36px 32px 56px;margin:auto}
.operator-report main{background:transparent;padding:0;border-radius:0;min-width:0}
.operator-report h1{font-size:clamp(26px,4vw,38px);letter-spacing:-.05em;margin:8px 0 12px;font-weight:750}
.operator-report h2{font-size:24px;letter-spacing:-.04em;border:0;margin:0 0 10px;padding:0}
.operator-report h3{font-size:18px;letter-spacing:-.025em;margin:26px 0 12px}
.operator-report h4{font-size:15px;margin:22px 0 10px}
.operator-report p{margin:10px 0;word-break:keep-all;overflow-wrap:anywhere}
.operator-report a{color:#176554;text-underline-offset:4px}
.report-header{display:flex;justify-content:space-between;align-items:center;gap:20px;border-top:3px solid #205e50;padding-top:22px}
.eyebrow{font-size:11px;letter-spacing:.16em;font-weight:750;color:#58716a}
.operator-report .metadata,.operator-report .muted{color:#5c6d66;font-size:13px}
.report-header .metadata{margin:0}
.report-mode{border:1px solid #cbd8d0;border-radius:30px;padding:6px 12px;font-size:12px;white-space:nowrap}
.operator-report .notice{border:1px solid #e5d7b9;border-left:3px solid #a78037;background:#faf5e8;padding:13px 16px;border-radius:8px;font-size:13px;color:#685226}
.operator-report .report-nav{position:sticky;top:0;z-index:5;display:grid;grid-template-columns:repeat(6,minmax(0,1fr)) .8fr;gap:4px;padding:7px;margin:26px 0 22px;border:1px solid #dce3dc;background:#fff;border-radius:12px;box-shadow:0 3px 12px #20333108}
.report-nav label{display:flex;align-items:center;justify-content:center;cursor:pointer;padding:11px 6px;border-radius:7px;font-size:13px;font-weight:650;color:#64756c;text-align:center}
.report-nav label:hover{background:#eef3ee;color:#205e50}
.page-choice{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
.report-page{display:none;min-width:0;background:white;padding:28px;border:1px solid #e0e6df;border-radius:14px}
.page-intro{color:#64756c;font-size:14px;margin-bottom:24px!important}
.page-number{display:block;font-size:11px;font-weight:700;letter-spacing:.13em;color:#5e7d6e;margin-bottom:7px}
.operator-report .cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:22px 0}
.operator-report .card{padding:19px 18px;background:#f4f7f2;border:1px solid #e4eae2;border-radius:10px;font-size:12px;color:#5a6d60;min-width:0}
.operator-report .card strong{font-size:clamp(17px,2.1vw,25px);letter-spacing:-.04em;color:#243e31;line-height:1.4;margin-top:8px;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.operator-report .card small{display:block;margin-top:6px;font-size:11px;line-height:1.5;color:#637268}
.overview-grid{display:grid;grid-template-columns:1.25fr 1fr;gap:18px;margin:22px 0}
.overview-block{padding:22px;border:1px solid #e0e6df;border-radius:10px;min-width:0}
.overview-block h3{margin:0 0 16px;font-size:15px}
.review-lead{font-size:20px;line-height:1.6;letter-spacing:-.03em;font-weight:650}
.status-list{margin:16px 0 0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.status-list dt{font-size:12px;color:#69796e;margin-bottom:3px}
.status-list dd{margin:0;font-weight:600;font-size:14px;overflow-wrap:anywhere}
.attention-list{padding:0;list-style:none;margin:0}
.attention-list li{padding:12px 0;border-bottom:1px solid #edf0e9;font-size:14px;overflow-wrap:anywhere}
.attention-list li:last-child{border:0}
.reading-note{padding:16px 18px;border-radius:9px;background:#f6f7f4;color:#627265;font-size:13px}
.operator-report .table-scroll{max-width:100%;overflow:auto;margin:18px 0;border:1px solid #e2e8df;border-radius:9px}
.operator-report table.report-table{display:table;border:0;width:100%;font-size:13px;margin:0;line-height:1.6}
.operator-report .report-table th{background:#f3f6f0;color:#526550;font-weight:650;font-size:12px;border:0;border-bottom:1px solid #dee6da;padding:13px 14px;min-width:70px}
.operator-report .report-table td{border:0;border-bottom:1px solid #edf0e9;padding:15px 14px;min-width:70px;white-space:pre-line}
.operator-report .report-table tr:last-child td{border-bottom:0}
.operator-report .report-table tr:hover td{background:#fafbf8}
.operator-report .thesis{padding:22px;border:1px solid #e0e6df;border-radius:11px;margin:18px 0;background:white;min-width:0}
.operator-report .thesis h3{margin:0 0 8px}
.review-scope{font-size:11px;font-weight:650;color:#597661;margin-bottom:5px;display:block}
.review-conclusion{font-size:15px;font-weight:600;line-height:1.8;padding-bottom:14px;border-bottom:1px solid #e8ede5}
.operator-report .thesis dl{grid-template-columns:130px minmax(0,1fr);font-size:14px;gap:12px}
.operator-report .thesis dt{color:#66755f;font-size:13px}
.operator-report .thesis dd{white-space:pre-line}
.operator-report details{border:1px solid #e0e6df;border-radius:9px;margin:14px 0;padding:0 16px;min-width:0}
.operator-report summary{padding:14px 0;color:#45624d;font-size:13px;font-weight:650}
.operator-report details[open]>summary{border-bottom:1px solid #e8ede5;margin-bottom:14px}
.operator-report details pre{font:13px/1.8 system-ui,sans-serif;white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f8f3;border-radius:6px;padding:14px}
.operator-report details .table-scroll{margin:14px 0}
.bar-chart{margin:22px 0;padding:22px 24px;background:#f6f8f3;border-radius:10px}
.bar-chart h3{margin:0 0 20px}
.bar-row{margin:16px 0}
.bar-label{display:flex;justify-content:space-between;gap:16px;font-size:13px;margin-bottom:8px}
.bar-label strong{font-variant-numeric:tabular-nums}
.bar-track{height:9px;background:#e2e9de;border-radius:10px;overflow:hidden}
.bar-fill{height:100%;background:#568469;border-radius:10px}
.chart-panel{padding:16px;background:#f8faf5;border:1px solid #e3eadd;border-radius:12px;margin:24px 0}
.operator-report .chart text{fill:#596c59;font-size:13px}
.chart-scroll{overflow:auto;max-width:100%}
.operator-report .chart{min-width:580px}
.report-footer{font-size:11px;color:#6c796f;margin-top:20px;display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}
.empty-state{padding:24px;border:1px dashed #d6e0d0;border-radius:9px;color:#64735e;font-size:14px;background:#fafbf8}
.operator-report .reading-document{background:white;padding:30px;border:1px solid #e0e6df;border-radius:14px;margin-top:22px}
.reading-document pre{white-space:pre-wrap;overflow-wrap:anywhere}
.reading-document p,.reading-document li{max-width:80ch}
.reading-document th{background:#f3f6f0;color:#526550}
.reading-document th,.reading-document td{padding:12px 14px;border-color:#e0e6da;vertical-align:top}
@media(max-width:850px){.operator-report .report-nav{grid-template-columns:repeat(4,minmax(0,1fr))}.overview-grid{grid-template-columns:1fr}.operator-report .cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:650px){
 body.operator-report{padding:18px 12px 30px;font-size:15px}
 .report-header{padding-top:14px;align-items:flex-start;gap:10px}
 .report-mode{font-size:11px;padding:5px 9px;margin-top:5px}
 .operator-report h1{font-size:25px}.operator-report h2{font-size:21px}
 .report-header .metadata{font-size:11px}.eyebrow{font-size:10px}
 .operator-report .report-nav{margin:18px 0 16px;gap:2px;padding:5px;position:relative}
 .report-nav label{font-size:12px;padding:10px 2px;min-height:40px}
 .report-page{padding:19px 15px;border-radius:11px}.page-intro{font-size:13px}
 .operator-report .cards{gap:8px;margin:18px 0}.operator-report .card{padding:14px 12px}.operator-report .card strong{font-size:19px}
 .overview-block{padding:17px}.overview-grid{gap:12px}.review-lead{font-size:18px}
 .operator-report .table-scroll{border:0;overflow:visible;margin:16px 0}
 .operator-report table.report-table,.operator-report .report-table tbody{display:block}
 .operator-report .report-table tr{border:1px solid #dfe6d9;margin:12px 0;padding:9px 12px;border-radius:9px;background:#fff}
 .operator-report .report-table td{display:grid;grid-template-columns:88px minmax(0,1fr);gap:12px;padding:8px 0;min-width:0;border:0;font-size:13px}
 .operator-report .report-table td:before{font-size:12px;color:#65765f;font-weight:500;white-space:normal}
 .operator-report .thesis{padding:16px}.operator-report .thesis dl{grid-template-columns:1fr;gap:4px}.operator-report .thesis dd{margin-bottom:12px}
 .operator-report details{padding:0 12px}.bar-chart{padding:16px}.bar-label{font-size:12px;flex-wrap:wrap;gap:4px}
 .chart-panel{padding:12px}.operator-report .reading-document{padding:20px 16px}
}
"""

# Native radio controls keep navigation usable in downloaded files without scripts.
for _key, _label in (*PAGES, ('all', '전체 보기')):
    OPERATOR_CSS += f'''
#view-{_key}:checked~.report-nav label[for="view-{_key}"]{{background:#245e4c;color:white}}
#view-{_key}:focus-visible~.report-nav label[for="view-{_key}"]{{outline:3px solid #be8836;outline-offset:2px}}
#view-{_key}:checked~.report-pages>#{_key}{{display:block}}
'''
OPERATOR_CSS += """
#view-all:checked~.report-pages>.report-page{display:block;margin-bottom:22px}
@media print{
 body.operator-report{padding:0;max-width:none;font-size:10pt;background:white}
 .operator-report .report-nav,.page-choice,.report-footer{display:none}
 .operator-report .report-page,.report-pages>.report-page{display:block!important;border:0;border-radius:0;padding:0;break-before:page}
 .report-pages>#overview{break-before:auto}
 .operator-report .cards{grid-template-columns:repeat(4,minmax(0,1fr))}
 .operator-report .overview-grid{grid-template-columns:1fr 1fr}
 .operator-report .table-scroll{overflow:visible;border:0}
 .operator-report table.report-table{display:table;font-size:8pt}
 .operator-report .report-table thead{display:table-header-group}
 .operator-report .report-table tbody{display:table-row-group}
 .operator-report .report-table tr{display:table-row;break-inside:avoid}
 .operator-report .report-table th,.operator-report .report-table td{display:table-cell;padding:7px;min-width:0}
 .operator-report .report-table td:before{display:none}
 .operator-report .chart{min-width:0}.chart-scroll{overflow:visible}
 .operator-report .thesis{break-inside:avoid}
}
"""
