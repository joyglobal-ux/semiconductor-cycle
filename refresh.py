"""반도체 사이클 — 데이터 수집/지표화 엔진.

핵심 성장률 지표를 출처별로 *각각* 그대로 수집하고, 그 위에 규칙 기반
'통합 해석'(국면 라벨 + 내러티브)을 한 층 얹어 data.js / data.json 을 생성한다.
컴포짓 점수로 뭉개지 않는다 — 각 숫자는 출처와 1:1 대조 검증 가능해야 한다.

데이터 등급
  auto         : 무료·키 불필요로 즉시 자동화   (SOX, FRED 美 생산)
  pending-key  : 무료 API 키 필요              (한국 수출 — ECOS_API_KEY)
  pending-input: 월 1회 수동 입력 (manual.json) (WSTS 글로벌 매출, DRAM 가격)

사용:
    uv run python refresh.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

HERE = Path(__file__).parent
HISTORY_MONTHS = 36  # 스파크라인에 보관할 월 수


# ─────────────────────────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────────────────────────
def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _yoy_from_monthly(s: pd.Series) -> pd.Series:
    """월별 인덱스(레벨) 시계열 → 전년동월비(%) 시계열."""
    s = s.sort_index()
    return (s.pct_change(12) * 100).dropna()


def _history(s: pd.Series, n: int = HISTORY_MONTHS) -> list[dict]:
    """시계열 → [{t:'YYYY-MM', v: float}] 최근 n개."""
    s = s.dropna().tail(n)
    return [{"t": t.strftime("%Y-%m"), "v": round(float(v), 2)} for t, v in s.items()]


def _direction(s: pd.Series, lookback: int = 3) -> str:
    """증감률 시계열의 방향(가속/둔화). 최신값 vs lookback개월 전."""
    s = s.dropna()
    if len(s) <= lookback:
        return "flat"
    now, prev = float(s.iloc[-1]), float(s.iloc[-1 - lookback])
    if now > prev + 0.3:
        return "up"
    if now < prev - 0.3:
        return "down"
    return "flat"


def _momentum(history: list[dict]) -> dict | None:
    """증감률 시계열에서 '방향이 꺾였는지/둔화됐는지'를 정밀 판정.

    레벨(현재 증감률)만이 아니라 그 변화(2차 미분)를 본다:
      mom    = 직전월 대비 변화(%p)  → 가장 빠른 가속/감속 신호
      slope3 = 3개월 기울기(%p)       → 추세 방향(노이즈 완화)
      정점/저점 통과 = 최근 창에서 고점/저점 대비 위치·경과개월
    이 셋으로 5국면(가속/가속 둔화/둔화/저점 통과/침체)을 라벨링한다.
    'mom<0 인데 slope3>0' = 추세는 상승이나 직전월 꺾임 → 조기 둔화 경보.
    """
    vs = [p["v"] for p in history]
    ts = [p["t"] for p in history]
    if len(vs) < 3:
        return None
    L = vs[-1]
    m1 = round(L - vs[-2], 1)                    # 직전치 대비
    s3 = round(L - vs[-min(4, len(vs))], 1)      # 최근 3구간 기울기
    pk_i = max(range(len(vs)), key=lambda k: vs[k])
    tr_i = min(range(len(vs)), key=lambda k: vs[k])
    last = len(vs) - 1
    eps = 0.5
    if s3 > eps:
        if L <= 0:
            phase, tone = "저점 통과", "warn-pos"
        elif m1 < -eps:
            phase, tone = "가속 둔화", "warn"   # 3M↑ 인데 직전월 꺾임 = 조기경보
        else:
            phase, tone = "가속", "pos"
    elif s3 < -eps:
        phase, tone = ("둔화", "warn") if L > 0 else ("침체", "neg")
    else:
        phase, tone = "정체", "neutral"
    return {
        "phase": phase,
        "tone": tone,
        "level": round(L, 1),
        "mom": m1,
        "slope3": s3,
        "peakValue": round(vs[pk_i], 1),
        "peakTime": ts[pk_i],
        "peakAgo": last - pk_i,
        "fromPeak": round(L - vs[pk_i], 1),
        "troughValue": round(vs[tr_i], 1),
        "troughTime": ts[tr_i],
        "troughAgo": last - tr_i,
        "fromTrough": round(L - vs[tr_i], 1),
        "peakIdx": pk_i,
        "troughIdx": tr_i,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1) FRED — 美 반도체 생산지수 (IPG3344S), 키 불필요 CSV
# ─────────────────────────────────────────────────────────────────────────────
def fetch_fred_semi_production() -> dict:
    sid = "IPG3344S"  # Industrial Production: Semiconductor & other electronic component (NAICS 3344)
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
    base = {
        "id": "us_semi_production",
        "label": "美 반도체 생산",
        "labelEn": "US semiconductor production",
        "unit": "YoY",
        "group": "growth",
        "role": "공급 측 보조",
        "source": "FRED · IPG3344S",
        "sourceUrl": f"https://fred.stlouisfed.org/series/{sid}",
        "status": "auto",
    }
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        from io import StringIO

        df = pd.read_csv(StringIO(r.text))
        date_col = "observation_date" if "observation_date" in df.columns else df.columns[0]
        df[date_col] = pd.to_datetime(df[date_col])
        df = df.set_index(date_col)
        s = pd.to_numeric(df[sid], errors="coerce").dropna()
        yoy = _yoy_from_monthly(s)
        base.update(
            value=round(float(yoy.iloc[-1]), 1),
            asOf=yoy.index[-1].strftime("%Y-%m"),
            dir=_direction(yoy),
            history=_history(yoy),
        )
        _log(f"  FRED {sid}: {base['value']}% YoY ({base['asOf']})")
    except Exception as e:  # noqa: BLE001
        _log(f"  ! FRED 실패: {e}")
        base.update(value=None, asOf=None, dir="flat", history=[], status="error")
    return base


# ─────────────────────────────────────────────────────────────────────────────
# 2) SOX — 필라델피아 반도체 지수, 키 불필요 (시장 신호)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_sox_momentum() -> dict:
    base = {
        "id": "sox_momentum",
        "label": "SOX 지수 모멘텀",
        "labelEn": "PHLX semiconductor index",
        "unit": "3M",
        "group": "market",
        "role": "시장의 선반영 신호",
        "source": "PHLX ^SOX · yfinance",
        "sourceUrl": "https://www.nasdaq.com/market-activity/index/sox",
        "status": "auto",
    }
    close = None
    try:
        import yfinance as yf

        df = yf.download("^SOX", period="6y", interval="1mo", progress=False, auto_adjust=True)
        if df is not None and not df.empty:
            close = df["Close"]
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
    except Exception as e:  # noqa: BLE001
        _log(f"  ! yfinance ^SOX 실패, fdr 폴백: {e}")
    if close is None or close.dropna().empty:
        try:
            import FinanceDataReader as fdr

            df = fdr.DataReader("SOXX")  # 인덱스 실패 시 ETF로 모멘텀 근사
            close = df["Close"].resample("ME").last()
            base["source"] = "SOXX ETF · fdr (대체)"
        except Exception as e:  # noqa: BLE001
            _log(f"  ! SOX 수집 전부 실패: {e}")
            base.update(value=None, asOf=None, dir="flat", history=[], status="error")
            return base
    close = close.dropna()
    mom3 = (close.pct_change(3) * 100).dropna()
    base.update(
        value=round(float(mom3.iloc[-1]), 1),
        asOf=mom3.index[-1].strftime("%Y-%m"),
        dir=_direction(mom3, lookback=2),
        history=_history(mom3),
    )
    _log(f"  SOX 3M momentum: {base['value']}% ({base['asOf']})")
    return base


# ─────────────────────────────────────────────────────────────────────────────
# 2.5) 개별 주가 모멘텀 — 하이닉스·삼전·MU·SNDK (일별, 키 불필요)
#      "가격 액션 = 정보": 1M/3M/12M 수익률 + 52주 고점 대비.
#      삼전-하이닉스 1M 스프레드 = 삼전 재진입 트리거 ①(RS 역전)의 상시 감시선.
# ─────────────────────────────────────────────────────────────────────────────
STOCK_DEFS = [
    # (yfinance ticker, fdr 폴백 코드, 라벨)
    ("000660.KS", "000660", "SK하이닉스"),
    ("005930.KS", "005930", "삼성전자"),
    ("MU", "MU", "Micron"),
    ("SNDK", "SNDK", "SanDisk"),
]


def _stock_close(yf_ticker: str, fdr_code: str) -> pd.Series | None:
    """일별 종가(수정주가) 시리즈. yfinance 실패 시 FinanceDataReader 폴백."""
    try:
        import yfinance as yf

        df = yf.download(yf_ticker, period="2y", interval="1d", progress=False, auto_adjust=True)
        if df is not None and not df.empty:
            c = df["Close"]
            if isinstance(c, pd.DataFrame):
                c = c.iloc[:, 0]
            c = c.dropna()
            if len(c) > 30:
                return c
    except Exception as e:  # noqa: BLE001
        _log(f"  ! yfinance {yf_ticker} 실패: {e}")
    try:
        import FinanceDataReader as fdr

        df = fdr.DataReader(fdr_code)
        c = df["Close"].dropna()
        if len(c) > 30:
            return c.tail(500)
    except Exception as e:  # noqa: BLE001
        _log(f"  ! fdr {fdr_code} 실패: {e}")
    return None


def fetch_stocks() -> dict | None:
    """주가 모멘텀 패널. 실패 종목은 건너뛰고, 전부 실패면 None(패널 미표시)."""
    items, as_of = [], None
    for yft, fdrc, label in STOCK_DEFS:
        c = _stock_close(yft, fdrc)
        if c is None:
            continue

        # 달력 기준 lookback — rs-screener(compute_returns.period_targets)와 동일 규약.
        # 21거래일 방식과 며칠 어긋나 수치가 달라지는 혼선 방지 (2026-07-05 통일).
        def ret(months: int) -> float | None:
            target = c.index[-1] - pd.DateOffset(months=months)
            prior = c[c.index <= target]
            if prior.empty:
                return None
            return round((float(c.iloc[-1]) / float(prior.iloc[-1]) - 1) * 100, 1)

        hi252 = float(c.tail(252).max())
        items.append(
            {
                "ticker": yft,
                "label": label,
                "r1m": ret(1),
                "r3m": ret(3),
                "r12m": ret(12),
                "fromHigh": round((float(c.iloc[-1]) / hi252 - 1) * 100, 1),
            }
        )
        t = c.index[-1]
        as_of = max(as_of, t) if as_of is not None else t
        _log(f"  {label}: 1M {items[-1]['r1m']}% · 3M {items[-1]['r3m']}% · 고점比 {items[-1]['fromHigh']}%")
    if not items:
        return None
    by = {i["label"]: i for i in items}
    spread = None
    if "삼성전자" in by and "SK하이닉스" in by and by["삼성전자"]["r1m"] is not None and by["SK하이닉스"]["r1m"] is not None:
        spread = round(by["삼성전자"]["r1m"] - by["SK하이닉스"]["r1m"], 1)
    return {
        "asOf": as_of.strftime("%Y-%m-%d") if as_of is not None else None,
        "items": items,
        "rsSpread1m": spread,          # 삼전 1M − 하이닉스 1M (%p). 양수 전환 = RS 역전
        "rsFlip": (spread is not None and spread > 0),
        "note": "삼전 재진입 트리거 = ① 1M RS 역전(스프레드 양수) AND ② 삼전 신고가 돌파(거래량). 고점比로 ②를 가늠.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3) 한국 반도체 수출 + DRAM 수출물가 — 한은 ECOS (무료 키: 환경변수 ECOS_API_KEY)
#    월별·자동. 키가 없으면 pending-key 상태로 자리만 잡는다.
# ─────────────────────────────────────────────────────────────────────────────
def _ecos_series(key: str, stat: str, item: str) -> pd.Series:
    """ECOS 월별 인덱스(레벨) 시계열을 받아 정렬·중복제거해 반환."""
    url = (
        f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/1000/"
        f"{stat}/M/200001/209912/{item}"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    payload = r.json()
    if "StatisticSearch" not in payload:
        raise RuntimeError(payload.get("RESULT", payload))
    rows = payload["StatisticSearch"]["row"]
    df = pd.DataFrame(rows)
    # 일부 표(예: 402Y016)는 TIME당 중복 행을 반환 → pct_change 가 어긋나므로 dedup 필수.
    df = df.drop_duplicates(subset="TIME", keep="last")
    df["t"] = pd.to_datetime(df["TIME"], format="%Y%m")
    df = df.set_index("t").sort_index()
    return pd.to_numeric(df["DATA_VALUE"], errors="coerce").dropna()


def _fill_from_ecos(base: dict, stat: str, item: str, log_name: str, mode: str = "yoy") -> dict:
    key = os.environ.get("ECOS_API_KEY")
    if not key:
        _log(f"  - {log_name}: ECOS_API_KEY 없음 → pending-key")
        return base
    try:
        s = _ecos_series(key, stat, item)
        chg = (s.pct_change(1) * 100).dropna() if mode == "mom" else _yoy_from_monthly(s)
        base.update(
            value=round(float(chg.iloc[-1]), 1),
            asOf=chg.index[-1].strftime("%Y-%m"),
            dir=_direction(chg),
            history=_history(chg),
            status="auto",
        )
        _log(f"  {log_name}: {base['value']}% {base.get('unit','')} ({base['asOf']})")
    except Exception as e:  # noqa: BLE001
        _log(f"  ! {log_name} ECOS 실패: {e} → pending-key 유지")
    return base


def fetch_kr_semi_exports() -> dict:
    # 확정 시리즈: 403Y001 수출금액지수(2020=100) / 30911AA '반도체' (월별).
    # 금액지수의 전년동월비 = 반도체 수출액 명목 증감률. (env 로 override 가능)
    base = {
        "id": "kr_semi_exports",
        "label": "한국 반도체 수출",
        "labelEn": "Korea semiconductor exports",
        "unit": "YoY",
        "group": "growth",
        "role": "선행지표 · 글로벌 수요 벨웨더",
        "source": "한국은행 ECOS · 수출금액지수(반도체)",
        "sourceUrl": "https://ecos.bok.or.kr/",
        "status": "pending-key",
        "value": None, "asOf": None, "dir": "flat", "history": [],
    }
    stat = os.environ.get("ECOS_SEMI_STAT") or "403Y001"
    item = os.environ.get("ECOS_SEMI_ITEM") or "30911AA"
    return _fill_from_ecos(base, stat, item, "한국 수출")


def fetch_dram_price() -> dict:
    # 402Y016 수출물가지수(2020=100) / 30911201AA 'DRAM' (월별). 분기 계약가 대신
    # 월별 자동 갱신. MoM(월별 가격변화) = 가장 빠른 DRAM 가격 신호 — 가격이 꺾이는
    # 순간을 즉시 포착(예: 4월 +25% → 5월 +7.6%).
    base = {
        "id": "dram_price",
        "label": "DRAM 수출물가",
        "labelEn": "DRAM export price",
        "unit": "MoM",
        "group": "growth",
        "role": "메모리 가격 사이클 · ⚠ 금액÷물량 블렌드 단가 — 제품 mix(HBM/저가 DDR 비중)에 왜곡될 수 있음, 고정거래가와 교차 확인",
        "source": "한국은행 ECOS · 수출물가지수(DRAM)",
        "sourceUrl": "https://ecos.bok.or.kr/",
        "status": "pending-key",
        "value": None, "asOf": None, "dir": "flat", "history": [],
    }
    return _fill_from_ecos(base, "402Y016", "30911201AA", "DRAM 수출물가", mode="mom")


# ─────────────────────────────────────────────────────────────────────────────
# 4) 수동 입력 지표 — WSTS 글로벌 매출, DRAM 고정거래가 (manual.json)
#    스크랩이 까다로워 월 1회 직접 입력. 값이 없으면 pending-input.
# ─────────────────────────────────────────────────────────────────────────────
MANUAL_DEFS = {
    "wsts_global_sales": {
        "label": "WSTS 글로벌 매출",
        "labelEn": "WSTS global semiconductor sales",
        "unit": "YoY",
        "group": "growth",
        "role": "업계 공식 기준점",
        "source": "WSTS / SIA 월간 보도자료",
        "sourceUrl": "https://www.semiconductors.org/category/news/global-sales-report/",
    },
    # DRAM 수출물가(블렌드)의 mix 왜곡을 보정하는 실거래 벤치마크.
    # TrendForce가 매월 말 발표하는 고정거래가(DDR5 16Gb 등 대표 품목) MoM을 입력.
    "dram_contract_price": {
        "label": "DRAM 고정거래가",
        "labelEn": "DRAM contract price (fixed)",
        "unit": "MoM",
        "group": "growth",
        "role": "계약가 — mix 왜곡 없는 가격 벤치마크 (수출물가 교차 검증용)",
        "source": "TrendForce 월말 고정거래가 (수동)",
        "sourceUrl": "https://www.trendforce.com/price/",
    },
}


def _load_prelim() -> dict | None:
    """관세청 순별(1~20일) 잠정 수출 속보 — 가장 빠른 신호. prelim.json (루틴 갱신)."""
    p = HERE / "prelim.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    d.pop("_comment", None)
    return d


def load_manual() -> list[dict]:
    path = HERE / "manual.json"
    raw = {}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for mid, meta in MANUAL_DEFS.items():
        entry = raw.get(mid, {})
        val = entry.get("value")
        out.append(
            {
                "id": mid,
                **meta,
                "value": val,
                "asOf": entry.get("asOf"),
                "dir": entry.get("dir", "flat"),
                "history": entry.get("history", []),
                "status": "manual" if val is not None else "pending-input",
            }
        )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 통합 해석 — 규칙 기반 국면 라벨 + 내러티브 (점수 합산 아님)
# ─────────────────────────────────────────────────────────────────────────────
RISING_PHASES = {"가속", "저점 통과"}
COOLING_PHASES = {"가속 둔화", "둔화", "침체"}


def interpret(indicators: list[dict]) -> dict:
    growth_all = [i for i in indicators if i["group"] == "growth"]
    growth = [i for i in growth_all if i.get("value") is not None]
    market = [i for i in indicators if i["group"] == "market" and i.get("value") is not None]
    live = [i for i in indicators if i.get("value") is not None]
    pending = [i for i in indicators if i.get("value") is None]

    def phase_of(ind: dict) -> str:
        return (ind.get("mom") or {}).get("phase", "")

    up = sum(1 for i in growth if phase_of(i) in RISING_PHASES)
    pos = sum(1 for i in growth if i["value"] > 0)
    cooling = [i for i in growth if phase_of(i) in COOLING_PHASES]
    n = len(growth)

    if n == 0:
        regime, regimeEn, tone = "데이터 대기", "No data", "neutral"
        headline = "자동 지표 연결 대기 중 — API 키/수동 입력을 채우면 해석이 활성화됩니다."
    else:
        pos_major = pos >= (n + 1) // 2
        rise_major = up >= (n + 1) // 2
        if pos_major and rise_major and not cooling:
            regime, regimeEn, tone = "확장 · 가속", "Expansion", "pos"
        elif pos_major and (cooling or not rise_major):
            regime, regimeEn, tone = "확장 · 둔화 조짐", "Expansion, cooling", "warn"
        elif not pos_major and rise_major:
            regime, regimeEn, tone = "회복 초입", "Early recovery", "warn-pos"
        else:
            regime, regimeEn, tone = "침체", "Contraction", "neg"
        headline = f"성장지표 {len(growth_all)}개 중 라이브 {n}개 — {pos}개 플러스 · {up}개 가속 → {regime}."
        if cooling:
            tags = ", ".join(f"{c['label']}({phase_of(c)})" for c in cooling)
            headline += f"  ⚠ 꺾임 감시: {tags}."

    bullets = []
    for i in live:
        m = i.get("mom") or {}
        bullets.append(
            {
                "label": i["label"],
                "value": i["value"],
                "unit": i["unit"],
                "phase": m.get("phase", ""),
                "tone": m.get("tone", "neutral"),
                "group": i["group"],
            }
        )

    return {
        "regime": regime,
        "regimeEn": regimeEn,
        "tone": tone,
        "headline": headline,
        "bullets": bullets,
        "growthTotal": len(growth_all),
        "growthLive": n,
        "growthPositive": pos,
        "growthRising": up,
        "marketLive": len(market),
        "coolingCount": len(cooling),
        "pendingCount": len(pending),
        "pendingNames": [p["label"] for p in pending],
        "liveCount": len(live),
    }


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    _log("=== 반도체 사이클 데이터 수집 ===")
    indicators = [
        fetch_kr_semi_exports(),   # 선행 (ECOS)
        fetch_dram_price(),        # DRAM 월별 가격 (ECOS)
        fetch_fred_semi_production(),
        fetch_sox_momentum(),
        *load_manual(),            # WSTS (수동)
    ]
    for ind in indicators:
        ind["mom"] = _momentum(ind.get("history") or [])
    interpretation = interpret(indicators)

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "indicators": indicators,
        "interpretation": interpretation,
        "prelim": _load_prelim(),
        "stocks": fetch_stocks(),
        "note": "각 지표는 출처에서 직접 검증 가능. 통합 해석은 점수 합산이 아니라 규칙 기반 국면 판정.",
    }

    (HERE / "data.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (HERE / "data.js").write_text(
        "window.SEMI_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )
    _log(f"\n완료 → {interpretation['regime']} · {interpretation['headline']}")
    _log("data.js / data.json 생성. index.html 을 브라우저에서 열어 확인.")


if __name__ == "__main__":
    main()
