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
# 3) 한국 반도체 수출 — 한은 ECOS (무료 키 필요: 환경변수 ECOS_API_KEY)
#    선행지표·핵심. 키가 없으면 pending-key 상태로 자리만 잡는다.
# ─────────────────────────────────────────────────────────────────────────────
def fetch_kr_semi_exports() -> dict:
    base = {
        "id": "kr_semi_exports",
        "label": "한국 반도체 수출",
        "labelEn": "Korea semiconductor exports",
        "unit": "YoY",
        "group": "growth",
        "role": "선행지표 · 글로벌 수요 벨웨더",
        "source": "한국은행 ECOS",
        "sourceUrl": "https://ecos.bok.or.kr/",
        "status": "pending-key",
        "value": None,
        "asOf": None,
        "dir": "flat",
        "history": [],
    }
    key = os.environ.get("ECOS_API_KEY")
    if not key:
        _log("  - 한국 수출: ECOS_API_KEY 없음 → pending-key (1분이면 무료 발급)")
        return base
    # ECOS StatisticSearch. STAT_CODE/ITEM 은 키 발급 후 StatisticTableList 로
    # '반도체 수출' 항목을 확인해 채운다(아래 값은 자리표시이며 첫 키 실행 때 확정).
    stat_code = os.environ.get("ECOS_SEMI_STAT", "")  # 예: 관세청 통관 수출 통계표
    item_code = os.environ.get("ECOS_SEMI_ITEM", "")
    if not stat_code:
        _log("  - 한국 수출: ECOS_SEMI_STAT 미설정 → pending-key")
        return base
    try:
        url = (
            f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/200/"
            f"{stat_code}/M/200001/209912/{item_code}"
        )
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        rows = r.json()["StatisticSearch"]["row"]
        df = pd.DataFrame(rows)
        df["t"] = pd.to_datetime(df["TIME"], format="%Y%m")
        df = df.set_index("t").sort_index()
        s = pd.to_numeric(df["DATA_VALUE"], errors="coerce").dropna()
        yoy = _yoy_from_monthly(s)
        base.update(
            value=round(float(yoy.iloc[-1]), 1),
            asOf=yoy.index[-1].strftime("%Y-%m"),
            dir=_direction(yoy),
            history=_history(yoy),
            status="auto",
        )
        _log(f"  한국 수출: {base['value']}% YoY ({base['asOf']})")
    except Exception as e:  # noqa: BLE001
        _log(f"  ! ECOS 실패: {e} → pending-key 유지")
    return base


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
    "dram_contract_price": {
        "label": "DRAM 고정거래가",
        "labelEn": "DRAM contract price",
        "unit": "MoM",
        "group": "growth",
        "role": "메모리 사이클 (삼성·하이닉스 직결)",
        "source": "TrendForce",
        "sourceUrl": "https://www.trendforce.com/price",
    },
}


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
def interpret(indicators: list[dict]) -> dict:
    growth = [i for i in indicators if i["group"] == "growth" and i.get("value") is not None]
    live = [i for i in indicators if i.get("value") is not None]

    def state(ind: dict) -> str:
        v, d = ind["value"], ind.get("dir", "flat")
        rising = d == "up"
        if v > 0:
            return "확장·가속" if rising else "확장·둔화"
        return "회복 신호" if rising else "위축"

    for ind in live:
        ind["state"] = state(ind)

    up = sum(1 for i in growth if i["value"] > 0)
    rising = sum(1 for i in growth if i.get("dir") == "up")
    n = len(growth)

    if n == 0:
        regime, regimeEn, tone = "데이터 대기", "No data", "neutral"
        headline = "자동 지표 연결 대기 중 — API 키/수동 입력을 채우면 해석이 활성화됩니다."
    else:
        pos_major = up >= (n + 1) // 2
        rise_major = rising >= (n + 1) // 2
        if pos_major and rise_major:
            regime, regimeEn, tone = "확장 · 가속", "Expansion", "pos"
        elif pos_major and not rise_major:
            regime, regimeEn, tone = "확장 · 둔화 (후기)", "Late expansion", "warn"
        elif not pos_major and rise_major:
            regime, regimeEn, tone = "회복 초입", "Early recovery", "warn-pos"
        else:
            regime, regimeEn, tone = "침체", "Contraction", "neg"
        headline = f"성장률 지표 {n}개 중 {up}개 플러스 · {rising}개 가속 → {regime}."

    bullets = []
    for i in live:
        sign = "+" if (i["value"] or 0) >= 0 else ""
        arrow = {"up": "가속", "down": "둔화", "flat": "보합"}[i.get("dir", "flat")]
        bullets.append(
            {
                "label": i["label"],
                "text": f"{sign}{i['value']}% {i['unit']} · {arrow}",
                "state": i.get("state", ""),
                "group": i["group"],
            }
        )

    return {
        "regime": regime,
        "regimeEn": regimeEn,
        "tone": tone,
        "headline": headline,
        "bullets": bullets,
        "growthCount": n,
        "growthPositive": up,
        "growthRising": rising,
        "liveCount": len(live),
    }


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    _log("=== 반도체 사이클 데이터 수집 ===")
    indicators = [
        fetch_kr_semi_exports(),   # 선행 (pending-key 가능)
        fetch_fred_semi_production(),
        fetch_sox_momentum(),
        *load_manual(),            # WSTS, DRAM
    ]
    interpretation = interpret(indicators)

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "indicators": indicators,
        "interpretation": interpretation,
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
