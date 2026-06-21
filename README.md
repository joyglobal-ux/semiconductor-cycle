# 반도체 사이클 (Semiconductor Cycle)

핵심 반도체 **성장률** 지표를 출처별로 *각각* 그대로 추적하고, 그 위에 규칙 기반
**통합 해석**(국면 라벨 + 내러티브) 한 층을 얹는 트래커. 컴포짓 점수로 뭉개지 않아
각 숫자를 출처와 1:1 대조 검증할 수 있다. 반도체는 사이클 산업이라 레벨보다
**방향(가속·둔화)** 을 본다.

라이브: https://joyglobal-ux.github.io/semiconductor-cycle/

상단에 **20일 잠정 속보**(관세청 순별 잠정 수출, 반도체 품목별 Y/Y·M/M)를 둔다 —
ECOS 월별 확정보다 ~10일 빠른 가장 빠른 선행 신호. `prelim.json` 에 저장하고
매월 22일 `kcs-prelim-update` 루틴이 관세청 보도자료에서 자동 갱신한다.

## 지표

| 지표 | 출처 | 단위 | 자동화 |
|---|---|---|---|
| 한국 반도체 수출 | ECOS 수출금액지수(반도체) 403Y001/30911AA | YoY | ✅ 자동(키) |
| DRAM 수출물가 | ECOS 수출물가지수(DRAM) 402Y016/30911201AA | MoM | ✅ 자동(키) |
| 美 반도체 생산 (IPG3344S) | FRED | YoY | ✅ 키 불필요 |
| SOX 지수 모멘텀 | PHLX ^SOX (yfinance) | 3M | ✅ 키 불필요 |
| WSTS 글로벌 매출 | WSTS / SIA 보도자료 | YoY | 월 1회 수동(스케줄 루틴 wsts-monthly-update가 매월 9일 자동 갱신) |

DRAM 은 분기 계약가(TrendForce, 느림) 대신 **ECOS 월별 DRAM 수출물가 MoM** 으로 받는다 —
가격이 꺾이는 순간을 가장 빨리 잡는 신호. WSTS 만 수동이며, 매월 9일 스케줄 루틴이
SIA 발표치를 검색해 `manual.json` 을 갱신한다.

## 실행

```bash
uv run python refresh.py      # data.js / data.json 재생성
```

`index.html` 은 `data.js`(`window.SEMI_DATA`)를 읽어 렌더한다. 정적 파일이라
로컬에서 `python -m http.server` 후 열거나 GitHub Pages 로 배포한다.

## 한국 수출 자동화 (선택)

선행지표라 가장 중요하다. 무료 키만 연결하면 활성화된다.

1. 한국은행 ECOS Open API 키 발급(무료·즉시): https://ecos.bok.or.kr/api/
2. 키 발급 후 `반도체 수출` 통계표/항목 코드를 확인
   (반도체 품목 세분화가 필요하면 관세청 UNIPASS 수출입무역통계 OpenAPI, HS 8541·8542 가 더 적합).
3. 환경변수 설정 후 실행:

```bash
export ECOS_API_KEY=...        # 또는 GitHub repo Secrets
export ECOS_SEMI_STAT=...      # 통계표 코드
export ECOS_SEMI_ITEM=...      # 항목 코드 (선택)
uv run python refresh.py
```

키가 없으면 해당 카드는 `키 발급 후 활성화` 상태로 자리만 잡는다(나머지는 정상 동작).

## 수동 입력 (WSTS · DRAM)

`manual.json` 의 `value` / `asOf` / `dir` 만 채우면 카드가 켜진다. **숫자를 지어내지
말고** 출처(SIA 보도자료·TrendForce) 확인 후 입력한다.

`history`(연속 3개 이상)를 채우면 국면 칩·차트·정밀 수치까지 표시된다. `value` 는
`history` 의 마지막 값과 일치시킨다.

```json
{ "wsts_global_sales": { "value": 93.9, "asOf": "2026-04",
    "history": [{"t":"2026-02","v":61.8},{"t":"2026-03","v":79.2},{"t":"2026-04","v":93.9}] } }
```

갱신 주기: WSTS 는 월 1회(SIA 보도자료, 매월 초), DRAM 은 분기 1회(TrendForce 계약가).

## 자동 갱신

`.github/workflows/refresh.yml` — SOX 는 매 거래일(美 장마감 후), 펀더멘털은 월초에
자동 수집·커밋. 한국 수출 자동화를 켜려면 repo Secrets 에 `ECOS_API_KEY`
(+ `ECOS_SEMI_STAT`, `ECOS_SEMI_ITEM`) 추가.

## 배포

별도 repo `semiconductor-cycle` → GitHub Pages → `joyglobal-ux.github.io/semiconductor-cycle/`.
허브(`joyglobal-ux.github.io`)에 카드가 `/semiconductor-cycle/` 로 연결돼 있다.
