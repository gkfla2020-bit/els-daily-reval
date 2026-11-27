"""한 번만 돌리는 준비 단계: 한국거래소 휴장일과 상품별 최초 기준가·낙인 여부를 정해 state/에 저장한다.

상품 조건은 국내에서 흔한 스텝다운 구조를 본뜬 [예시]이고, 최초 기준가는 발행일의 실제 종가다.
- 코스피200 과거 종가: 네이버 금융 차트 데이터(한 번만 조회, 원자료는 저장하지 않음)
- HSCEI 과거 종가: Yahoo Finance ^HSCE
낙인 여부는 발행일 다음 날부터 cutoff(평가 시작 전날)까지 매일 종가로 확인한다(두 지수 중 더 많이 빠진 쪽).

사용법: python tools/setup_products.py 2026-11-26
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from els_daily import data, model  # noqa: E402

NAVER = "https://fchart.stock.naver.com/sise.nhn?symbol=KPI200&timeframe=day&count=400&requestType=0"

SPECS = [
    dict(id="ELS-01", issue="2026-11-02", barriers=[0.90, 0.90, 0.85, 0.85, 0.80, 0.75], coupon=0.070, ki=0.50,
         note="최근 발행, 기본 구조"),
    dict(id="ELS-02", issue="2026-06-26", barriers=[0.90, 0.90, 0.85, 0.85, 0.80, 0.75], coupon=0.070, ki=0.50,
         note="코스피200 고점 부근 발행"),
    dict(id="ELS-03", issue="2026-09-22", barriers=[0.85, 0.85, 0.80, 0.80, 0.75, 0.70], coupon=0.060, ki=0.45,
         note="상환 조건·낙인을 낮춘 안정형"),
    dict(id="ELS-04", issue="2026-08-10", barriers=[0.90, 0.85, 0.80, 0.75, 0.70, 0.65], coupon=0.080, ki=0.55,
         note="낙인이 높은 고수익형"),
    dict(id="ELS-05", issue="2026-05-13", barriers=[0.90, 0.90, 0.85, 0.85, 0.80, 0.75], coupon=0.075, ki=0.60,
         note="1차 평가일 지남, 낙인 60%"),
]


def naver_k200() -> dict[str, float]:
    req = urllib.request.Request(NAVER, headers=data.UA)
    body = urllib.request.urlopen(req, timeout=40).read().decode("euc-kr", "ignore")
    rows = re.findall(r'data="(\d{8})\|[^|]*\|[^|]*\|[^|]*\|([\d.]+)\|', body)
    return {f"{d[:4]}-{d[4:6]}-{d[6:]}": float(c) for d, c in rows}


def main(cutoff: str) -> None:
    hol = {str(y): data.krx_holidays(y) for y in (2026, 2027, 2028)}
    (model.STATE).mkdir(exist_ok=True)
    (model.STATE / "krx_holidays.json").write_text(json.dumps(hol, ensure_ascii=False, indent=1), encoding="utf-8")
    cal = model.krx_calendar()

    K = naver_k200()
    H = dict(data.yahoo_series("^HSCE", "2y")[0])
    days = sorted(d for d in set(K) | set(H) if d <= cutoff)
    out = []
    for s in SPECS:
        k0, h0 = K[s["issue"]], H[s["issue"]]
        p = model.Product.build({**s, "initial": [k0, h0]}, cal)
        lk, lh, worst_min, worst_day, redeemed = k0, h0, 1.0, s["issue"], None
        for d in days:
            if d <= s["issue"]:
                continue
            lk, lh = K.get(d, lk), H.get(d, lh)
            w = min(lk / k0, lh / h0)
            if w < worst_min:
                worst_min, worst_day = w, d
            if d in p.obs_dates and redeemed is None:
                k = p.obs_dates.index(d)
                if w >= p.barriers[k]:
                    redeemed = {"date": d, "k": k, "amount": model.redemption_amount(p, k)}
        out.append({**s, "initial": [k0, h0],
                    "initial_source": "코스피200: 네이버 금융 차트(KPI200) 발행일 종가 / HSCEI: Yahoo Finance ^HSCE 발행일 종가",
                    "obs_dates": list(p.obs_dates),
                    "state": {"asof": cutoff, "ki_hit": worst_min < s["ki"], "worst_min": worst_min, "worst_min_day": worst_day,
                              "redeemed": redeemed}})
        print(s["id"], s["issue"], k0, round(h0, 2), "최저", round(worst_min, 4), worst_day, "낙인" if worst_min < s["ki"] else "", redeemed or "")
    (model.STATE / "products.json").write_text(json.dumps({"note": "상품 조건은 [예시], 최초 기준가는 발행일 실제 종가",
                                                            "products": out}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1])
