# 전략 매핑 규칙

## 조사 기준

- 확인 저장소: `koreainvestment/kis-ai-extensions`, `koreainvestment/open-trading-api`
- 확인 파일: `shared/skills/kis-strategy-builder/SKILL.md`, `shared/skills/kis-backtester/SKILL.md`, `shared/skills/kis-backtester/references/yaml-templates.md`, `strategy_builder/frontend/src/lib/builder/constants.ts`, `strategy_builder/README.md`, `backtester/README.md`
- `kis-ai-extensions`의 백테스터 MCP 도구는 `run_backtest_tool`, `run_preset_backtest_tool`, `get_backtest_result_tool`, `retry_backtest_tool`, `get_report_tool`, `list_presets_tool`, `get_preset_yaml_tool`, `validate_yaml_tool`, `list_indicators_tool`, `optimize_strategy_tool`, `run_batch_backtest_tool`로 문서화되어 있다.
- README 표에는 `run_backtest`, `optimize_params`, `get_report`, `list_strategies`가 노출된다. 실제 스킬 문서에서 쓰는 도구명은 `_tool` 접미사가 붙은 이름이므로 실행 전 MCP 목록으로 다시 확인한다.

## 프리셋 전략 매핑

### 백테스터 프리셋 10종

| 전략 ID | 실제 이름 | 주요 파라미터 | 단기 가중 | 중기 가중 | 장기 가중 | 신호 해석 |
|---|---|---|---:|---:|---:|---|
| `sma_crossover` | SMA 골든/데드 크로스 | `fast_period`, `slow_period` | 2 | 5 | 3 | 단기선 상향 돌파는 BUY, 하향 돌파는 SELL |
| `momentum` | 모멘텀 | `lookback`, `threshold` | 5 | 4 | 1 | 최근 수익률이 임계치 이상이면 BUY, 약하면 HOLD/SELL |
| `trend_filter_signal` | 추세 필터 + 시그널 | `trend_period` | 3 | 5 | 3 | 장기 추세 위에서만 매수 신호를 신뢰 |
| `week52_high` | 52주 신고가 돌파 | `lookback`, `stop_loss_pct` | 3 | 5 | 2 | 신고가 돌파는 추세 지속 BUY, 실패하면 위험 |
| `ma_divergence` | 이동평균 이격도 | `period`, `buy_ratio`, `sell_ratio` | 2 | 4 | 3 | 과도한 하방 이격은 BUY 후보, 과열 이격은 SELL 후보 |
| `false_breakout` | 추세 돌파 후 이탈 | `lookback` | 5 | 3 | 1 | 전고점 돌파 후 이탈하면 SELL 또는 손절 |
| `short_term_reversal` | 단기 반전 | `period`, `threshold_pct` | 4 | 2 | 2 | 단기 과매도 후 반등은 BUY, 과열 후 꺾임은 SELL |
| `strong_close` | 강한 종가 | `close_ratio`, `stop_loss_pct` | 5 | 3 | 1 | 고가 근처 마감은 단기 BUY 강도 증가 |
| `volatility_breakout` | 변동성 축소 후 확장 | `atr_period`, `lookback` | 5 | 4 | 1 | 변동성 재확대와 방향성 동행 시 BUY |
| `consecutive_moves` | 연속 상승·하락 | `up_days`, `down_days` | 4 | 3 | 1 | 연속 상승은 추세 BUY, 연속 하락은 SELL 또는 역발상 후보 |

### 전략 빌더 프리셋 10종

| 전략 ID | 실제 이름 | 카테고리 | 주요 지표 | 비고 |
|---|---|---|---|---|
| `golden_cross` | 골든크로스 | `trend` | `SMA(50)`, `SMA(200)` | `.kis.yaml` 템플릿 제공 |
| `adx_trend` | ADX 강한 추세 | `trend` | `ADX(14)` | 추세 강도 필터 |
| `obv_divergence` | OBV 다이버전스 | `volume` | `OBV` | 거래량 추세 확인 |
| `mfi_oversold` | MFI 과매도 | `oscillator` | `MFI(14)` | 수급 포함 과매도 |
| `vwap_bounce` | VWAP 반등 | `trend` | `VWAP` | 평균 거래가격 회복 |
| `cci_reversal` | CCI 반전 | `oscillator` | `CCI(20)` | 반전형 |
| `williams_reversal` | Williams %R 반전 | `oscillator` | `Williams%R(14)` | 과매도/과매수 |
| `atr_breakout` | ATR 변동성 돌파 | `volatility` | `ATR(14)` | 변동성 돌파 |
| `disparity_mean_revert` | 이격도 평균회귀 | `mean_reversion` | `Disparity(20)` | 평균회귀 |
| `consecutive_candle` | 연속 캔들 패턴 | `momentum` | `Consecutive(3)` | 연속 캔들 |

## 핵심 기술지표 호출 방법

| 지표 | `.kis.yaml` indicator id | 기본 파라미터 | 조건 예시 | 반환/출력 |
|---|---|---|---|---|
| 단순이동평균 | `sma` | `period` | `{indicator: fast, operator: cross_above, compare_to: slow}` | 단일 값 |
| 지수이동평균 | `ema` | `period` | `{indicator: ema20, operator: greater_than, compare_to: ema60}` | 단일 값 |
| RSI | `rsi` | `period: 14` | `{indicator: rsi, operator: less_than, value: 30}` | 단일 값 |
| MACD | `macd` | `fast: 12`, `slow: 26`, `signal: 9` | `output: value`, `compare_to: macd`, `compare_output: signal`, `operator: cross_above` | `value`, `signal`, `histogram` |
| 볼린저밴드 | `bollinger` | `period`, `std` 또는 `k` | 가격과 `upper`, `middle`, `lower` 비교 | `upper`, `middle`, `lower`. 정확한 출력명은 실행 시점 MCP 응답으로 확인 |
| ATR | `atr` | `period: 14` | 변동성 돌파 필터 | 단일 값 |
| ROC | `roc` | `period` | `{indicator: daily_return, operator: greater_than, value: 0}` | 단일 값 |
| MFI | `mfi` | `period: 14` | `{indicator: mfi, operator: cross_above, value: 20}` | 단일 값 |
| CCI | `cci` | `period: 20` | `{indicator: cci, operator: cross_above, value: -100}` | 단일 값 |
| Williams %R | `williams_r` | `period: 14` | `{indicator: wr, operator: cross_above, value: -80}` | 단일 값 |
| VWAP | `vwap` | `period` | `{indicator: close, operator: cross_above, compare_to: vwap}` | 단일 값 |
| OBV | `obv` | 없음 | 가격 추세 필터와 병행 | 단일 값 |

## `.kis.yaml` 작성 규약

```yaml
version: "1.0"
metadata:
  name: 전략명
  description: 전략 설명
  category: trend
  author: user
strategy:
  id: snake_case_id
  indicators:
    - id: rsi
      alias: rsi
      params:
        period: 14
  entry:
    logic: AND
    conditions:
      - indicator: rsi
        operator: less_than
        value: 30
  exit:
    logic: AND
    conditions:
      - indicator: rsi
        operator: greater_than
        value: 70
risk:
  stop_loss:
    enabled: true
    percent: 3.0
  take_profit:
    enabled: true
    percent: 8.0
```

## 신호 출력 규약

| 필드 | 규약 |
|---|---|
| `action` | `BUY`, `SELL`, `HOLD`, 오류 시 `ERROR` |
| `strength` | 0~1 강도. 0.5 미만은 리포트상 주문 검토에서도 제외 |
| `reason` | 신호의 핵심 조건 설명. 예: `RSI 28.3 < 30` |
| 주문 가능 신호 | `BUY`, `SELL`만 리포트상 주문 검토 대상. 이 스킬은 실제 주문을 실행하지 않는다. |
| 관망 | 조건 미충족 또는 근거 약함은 `HOLD` |

## 실행 절차

1. `list_presets_tool` 또는 `list_indicators_tool`로 현재 MCP 서버의 실제 목록을 확인한다.
2. 프리셋은 `run_preset_backtest_tool`로 실행하고, 사용자 정의 전략은 `validate_yaml_tool` 통과 후 `run_backtest_tool`로 실행한다.
3. 결과는 `get_backtest_result_tool`로 수집하고, 필요한 경우 `get_report_tool`로 HTML/JSON 리포트를 받는다.
4. 도구명이 README 표기와 다르면 MCP 응답의 실제 도구명을 우선한다.
