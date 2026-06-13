# Portfolio Report Template

Write one Korean report at `reports/YYYY-MM-DD_포트폴리오.md`.

```markdown
# 포트폴리오 평결문 - YYYY-MM-DD

## 실행 정보
- run_id:
- 작업 시작:
- 환경: demo / real / 분석 전용
- 최종 상태: success / partial / failed

## 1. 수집 상태
| 도메인 | 상태 | 전체 종목 수 | 오류 종목 수 | 핵심 오류 |
|---|---|---:|---:|---|
| 시장 | | | | |
| 재무 | | | | |
| 뉴스 | | | | |

## 2. 평결 제외 종목
| 종목식별자 | 종목명 | 제외 사유 | 누락 필수 정보 |
|---|---|---|---|

## 3. `decision-brief.json` 요약
- `decision-brief.json` 생성 여부:
- 포함된 eligible 종목 수:
- price-only eligible 종목 수:
- 제외된 raw payload / 기사 원문 / 민감정보:
- 핵심 누락 또는 오류:

## 4. `first-verdict` 독립 평결
| 종목식별자 | 종목명 | 원점수 평균(0-10) | 확신도 보정 최종점수(0-10) | 유효 응답 수 | 핵심 근거 | 핵심 리스크 |
|---|---|---:|---:|---:|---|---|

## 5. `second-verdict` 포트폴리오 평결
- 중기 시장 판단:
- 목표 현금 금액:
- 현금 판단 코드:
- 포트폴리오 한 줄 판단:
- Main agent 검증 결과:

| 종목식별자 | 종목명 | 현재 보유수량 | 목표 보유수량 | 상대매력도 | 판단 코드 | 한 줄 판단 |
|---|---|---:|---:|---:|---|---|

## 6. 최신 계좌 검증
- 총자산:
- 현금 또는 주문가능금액:
- 미체결 매수/매도:
- 예약 매수/매도:
- 당일 체결:

## 7. 기존 미체결·예약 주문 조정
| 종목식별자 | 종목명 | 기존 주문번호 | 구분 | 방향 | 잔여수량 | 가격 | 주문유형 | 경로 | 조치 | 사유 | 결과 | 확인 상태 | 대체 주문번호 |
|---|---|---|---|---|---:|---:|---|---|---|---|---|---|---|

## 8. 최종 주문 목록
| 종목식별자 | 종목명 | 방향 | 현재 실시간 보유수량 | 미체결·예약 매수 | 미체결·예약 매도 | 예상 보유수량 | 목표 보유수량 | 추가 필요수량 | 결과 |
|---|---|---|---:|---:|---:|---:|---:|---:|---|

## 9. 실행 결과
- 주문 요청 유형: 분석만 / 준비 / 모의 제출 / 실전 제출
- 실제 제출 여부:
- 제출된 주문번호 또는 예약번호:
- 취소/정정 요청번호:
- 취소/정정 확인 상태:
- 실패 또는 보류 사유:

## 10. 아티팩트
- 실행 디렉터리: reports/runs/<run_id>/
- 보존된 partial / failed 아티팩트:
- `decision-brief.json`:

## 11. 메모
- 당일 체결수량은 현재 보유수량에 이미 반영된 값으로 보고 다시 차감하지 않음
- `second-verdict`는 단일 `judge-midterm` target을 사용하며 Main agent가 총자산/현금/집중도/active 주문/same-day/market open gate를 검증함
- 투자 권유가 아니라 의사결정 보조 분석임
```

Do not omit failed or excluded symbols. Summarize sensitive account data without exposing account numbers, tokens, app keys, app secrets, or HTS IDs.
