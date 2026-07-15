# Judge Debate Output Format

Bull/Bear는 Python pipeline이 관리하는 동일 Codex session에서 `opening` → `rebuttal-1`까지 응답한다. 각 turn은 JSON object 하나만 반환한다.

```json
{
  "stage": "judge-debate",
  "phase": "opening|rebuttal-1",
  "side": "bull|bear",
  "symbols": [
    {
      "symbol_id": "005930",
      "symbol_name": "삼성전자",
      "arguments": [
        {
          "argument_id": "005930-bull-opening-1",
          "kind": "claim|rebuttal",
          "targets": [],
          "statement": "짧고 검증 가능한 명시적 논거",
          "evidence_refs": ["analyst-review:005930:analyst-quality-value"]
        }
      ],
      "concessions": [],
      "unresolved_conflicts": [],
      "final_position": "",
      "recommended_action": "buy|hold|sell",
      "target_holding_quantity": 0
    }
  ],
  "errors": []
}
```

## 공통 규칙

- 후보 전체를 한 응답에서 다루고 `symbol_ids` 밖의 종목을 반환하지 않는다.
- `argument_id`는 같은 run에서 유일하고 phase별 고정 접두사를 사용한다.
  - opening: `<symbol_id>-<side>-opening-<number>`
  - rebuttal-1: `<symbol_id>-<side>-rebuttal-1-<number>`
- `statement`에는 사후 분석 가능한 명시적 판단 근거만 쓴다. 숨은 사고과정이나 장문의 독백을 반환하지 않는다.
- `evidence_refs`에는 제공된 artifact에서 확인 가능한 field 또는 analyst view 식별자를 적는다. 외부 자료나 optional evidence 부재를 참조하지 않는다.
- `concessions`와 `unresolved_conflicts`는 항상 string array로 반환한다.
- Markdown, 코드펜스, 표, raw source payload를 반환하지 않는다.

## Phase 규칙

- `opening`: `kind=claim`, `targets=[]`. 독립적인 핵심 주장과 스스로 인정하는 약점을 명시한다.
- `rebuttal-1`: `kind=rebuttal`. 각 `targets`는 상대 opening의 `argument_id`를 하나 이상 참조한다. 상대와 무관한 새 주장을 만들지 않고 `final_position`, `recommended_action`, `target_holding_quantity`를 반드시 완성한다.
- `recommended_action`은 `portfolio_snapshot.current_live_holding_quantity`와 `target_holding_quantity`의 차이에 따라 `buy`, `hold`, `sell` 중 하나로 일치시킨다. snapshot이 없으면 기준 수량은 0이다.
- `target_holding_quantity`는 0 이상의 정수다. 매수 후보는 기준 수량보다 줄일 수 없고 매도 후보는 기준 수량보다 늘릴 수 없다. 근거가 부족하면 기준 수량을 유지하고 `hold`를 반환한다.
