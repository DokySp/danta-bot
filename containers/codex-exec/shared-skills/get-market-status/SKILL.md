---
name: get-market-status
description: "Report market direction, percent change, and a brief Korean opinion for S&P 500, Nasdaq, Dow, KOSPI, and KOSDAQ, using Google Finance for US indexes."
---

# Get Market Status

## Purpose

Use this skill when the user asks for the current market status or calls `$get-market-status`.

## Markets

Always report these five markets separately:

- S&P 500
- Nasdaq
- Dow
- KOSPI
- KOSDAQ

## Data Rules

- Use live or latest-available market data at the time of the call.
- For US indexes, use Google Finance (`https://www.google.com/finance/`) as the authoritative quote source:
  - S&P 500: `https://www.google.com/finance/quote/.INX:INDEXSP`
  - Nasdaq: `https://www.google.com/finance/quote/.IXIC:INDEXNASDAQ`
  - Dow: `https://www.google.com/finance/quote/.DJI:INDEXDJX`
- Do not mix US index values or percent changes from Yahoo Finance, MarketWatch, Investing.com, news snippets, futures, ETFs, or another source when Google Finance data is available.
- If Google Finance is unavailable for a US index, mark that market as `확인불가` instead of falling back to another source.
- For KOSPI and KOSDAQ, prefer KIS MCP index APIs or an official KRX source.
- Use the index's percent change versus the previous close as `등락률`.
- Classify status from percent change:
  - `상승`: percent change is greater than `0.1%`.
  - `하강`: percent change is less than `-0.1%`.
  - `보합`: percent change is between `-0.1%` and `0.1%`, inclusive.
- If a market is closed, use the latest available regular-session index value and state that in the opinion.
- If a live quote cannot be verified for a market, do not guess. Write `상태: 확인불가`, `등락률: 확인불가`, and explain the missing data in `의견`.
- Do not use futures as a substitute for the spot index unless the user explicitly asks for futures.
- Do not provide trading orders or portfolio actions from this skill.

## Output Format

Return only the following Korean Markdown format. Do not add tables, JSON, YAML, preambles, or extra sections.

```markdown
S&P 500:
- 상태: 상승/하강/보합/확인불가
- 등락률: +0.00%
- 의견: ...

Nasdaq:
- 상태: 상승/하강/보합/확인불가
- 등락률: +0.00%
- 의견: ...

Dow:
- 상태: 상승/하강/보합/확인불가
- 등락률: +0.00%
- 의견: ...

코스피:
- 상태: 상승/하강/보합/확인불가
- 등락률: +0.00%
- 의견: ...

코스닥:
- 상태: 상승/하강/보합/확인불가
- 등락률: +0.00%
- 의견: ...
```

## Opinion Rules

- Keep each opinion to one concise Korean sentence.
- Base the opinion on the observed percent change, market session state, and whether the move is broad or mild.
- Mention when the value is from the latest close instead of an open live session.
- Do not cite unverifiable causes such as news, rates, or policy unless you verified that evidence during the same call.
