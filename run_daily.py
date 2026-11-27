"""ELS 일일 재평가: 장 마감 뒤 시장데이터를 받아 상품별로 다시 평가하고, 전일 대비 변동을 입력별로 나눈다.

결과: state/history.json(날짜별 기록), docs/runs/<날짜>/ 엑셀·JSON, docs/index.html(웹 화면), docs/latest.json.

사용법: python run_daily.py [--where 로컬|GitHub Actions] [--force]
  --force  장 마감(15:45 KST) 전에도 실행(시험용, 기록에 '시험 실행'으로 남김)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import QuantLib as ql

from els_daily import data, model, report
from els_daily.model import Inputs

ROOT = Path(__file__).resolve().parent
HISTORY = model.STATE / "history.json"
LABELS = ("코스피200 종가", "VKOSPI 종가", "HSCEI 종가", "HSCEI 변동성(60일 실현)", "국고채 3년")


def load_history() -> list[dict]:
    return json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else []


def save_history(h: list[dict]) -> None:
    HISTORY.write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding="utf-8")


def asof(rows: list, d: str):
    """rows=[(YYYY-MM-DD, 값)]에서 d 이하 마지막 값."""
    ok = [r for r in rows if r[0] <= d]
    return ok[-1] if ok else (None, None)


def hscei_asof(rows: list, d: str, window: int = 60) -> tuple[float, str, float]:
    ok = [r for r in rows if r[0] <= d]
    px = np.array([c for _, c in ok])
    lr = np.diff(np.log(px))[-window:]
    return ok[-1][1], ok[-1][0], float(lr.std(ddof=1) * math.sqrt(252) * 100)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--where", default="GitHub Actions" if os.environ.get("GITHUB_ACTIONS") else "로컬")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    t_start = time.time()
    now = data.now_kst()
    today = now.date()
    cal = model.krx_calendar()
    qt = model.qd(today)
    ql.Settings.instance().evaluationDate = qt
    if not cal.isBusinessDay(qt):
        print(f"{today}: 한국거래소 휴장일이라 평가하지 않음")
        return 0
    trial = (now.hour, now.minute) < (15, 45)
    if trial and not a.force:
        print("장 마감(15:45 KST) 전입니다. 시험 실행은 --force")
        return 1
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"

    # ── 1. 입력 수집 ──────────────────────────────────────────────────────────
    q = data.krx_indices(today)
    hc, hv = data.hscei(today)
    q.update({"HSCEI 종가": hc, "HSCEI 변동성(60일 실현)": hv, "국고채 3년": data.ktb3y(today)})
    cross = data.yahoo_ks200(today)
    missing = [k for k in LABELS if q[k].value is None]

    history = [h for h in load_history() if h["date"] != today.isoformat()]   # 같은 날 다시 돌리면 덮어씀
    prev_run = next((h for h in reversed(history) if h["date"] < today.isoformat() and h["status"] == "완료"), None)
    prev_date = prev_run["date"] if prev_run else model.iso(cal.advance(qt, -1, ql.Days))

    entry = {"date": today.isoformat(), "run_at": now.strftime("%Y-%m-%d %H:%M:%S KST"), "where": a.where,
             "run_url": run_url, "trial": trial, "prev_date": prev_date,
             "quotes": {k: {kk: vv for kk, vv in v.to_dict().items() if kk != "extra"} | {"extra": {kk: vv for kk, vv in v.extra.items() if kk != "rows"}}
                        for k, v in {**q, "코스피200 (교차 확인)": cross}.items()}}
    if missing:
        entry.update(status="수집 실패", missing=missing, products={}, checks=[], seconds=round(time.time() - t_start, 1))
        history.append(entry)
        save_history(history)
        report.write_all(history, cal)
        print("수집 실패:", missing)
        return 2

    x1 = Inputs(k200=q["코스피200 종가"].value, hscei=q["HSCEI 종가"].value, vkospi=q["VKOSPI 종가"].value,
                vol_h=q["HSCEI 변동성(60일 실현)"].value, ktb3y=q["국고채 3년"].value)
    if prev_run:
        x0 = Inputs(**prev_run["inputs"])
        prev_from = "전일 실행 기록"
    else:                                      # 첫 실행: 전일 입력을 같은 출처의 전일 값으로 다시 구성
        h_rows = q["HSCEI 종가"].extra["rows"]
        h0, _, vh0 = hscei_asof(h_rows, prev_date)
        r0 = asof(q["국고채 3년"].extra["rows"], prev_date)[1]
        x0 = Inputs(k200=q["코스피200 종가"].prev_value, hscei=h0, vkospi=q["VKOSPI 종가"].prev_value, vol_h=vh0, ktb3y=r0)
        prev_from = "첫 실행이라 출처의 전일 값으로 구성"
    entry.update(inputs=asdict(x1), prev_inputs=asdict(x0), prev_from=prev_from)

    # ── 2. 상품별 재평가 ──────────────────────────────────────────────────────
    products = model.load_products(cal)
    specs = {s["id"]: s for s in json.loads((model.STATE / "products.json").read_text(encoding="utf-8"))["products"]}
    n_max = max(p.n_steps for p in products)
    z = model.normals(n_max, model.SEED)
    z_alt = model.normals(n_max, model.SEED_ALT)
    res = {}
    for p in products:
        st0 = prev_run["products"][p.id]["state"] if prev_run and p.id in prev_run["products"] else specs[p.id]["state"]
        e0, e1 = model.bdays(cal, model.qd(p.issue), model.qd(prev_date)), model.bdays(cal, model.qd(p.issue), qt)
        pf = model.perf(p, x1)
        w1 = min(pf)
        st1 = {"asof": today.isoformat(), "ki_hit": st0["ki_hit"] or w1 < p.ki, "worst_min": min(st0["worst_min"], w1),
               "worst_min_day": today.isoformat() if w1 < st0["worst_min"] else st0["worst_min_day"], "redeemed": st0["redeemed"]}
        row = {"state": st1, "perf": pf, "worst": w1, "elapsed": e1, "terms": p.terms(), "issue": p.issue}
        nxt = [(d, b) for d, b in zip(p.obs_dates, p.barriers) if d >= today.isoformat()]
        row["next_obs"] = {"date": nxt[0][0], "barrier": nxt[0][1], "bdays": model.bdays(cal, qt, model.qd(nxt[0][0]))} if nxt else None
        if st0["redeemed"]:
            row.update(status="상환 완료", value=None)
            res[p.id] = row
            continue
        recorded_prev = prev_run["products"][p.id]["value"] if prev_run and p.id in prev_run["products"] else None
        if today.isoformat() in p.obs_dates and w1 >= p.barriers[p.obs_dates.index(today.isoformat())]:
            k = p.obs_dates.index(today.isoformat())
            amt = model.redemption_amount(p, k)
            v0 = model.value(p, x0, e0, st0["ki_hit"], z)[0]
            st1["redeemed"] = {"date": today.isoformat(), "k": k, "amount": amt}
            att = dict(prev=v0, prev_se=0.0, today=amt, today_se=0.0, underlying=amt - v0, vol=0.0, rate=0.0, time=0.0)
            row["status"] = f"{k + 1}차 조기상환"
        else:
            att = model.attribution(p, x0, e0, st0["ki_hit"], x1, e1, st1["ki_hit"], z)
            row["status"] = "평가"
        prev_shown = recorded_prev if recorded_prev is not None else att["prev"]
        effects = att["underlying"] + att["vol"] + att["rate"] + att["time"]
        row.update(value=att["today"], se=att["today_se"], prev_value=prev_shown, prev_recalc=att["prev"],
                   change=att["today"] - prev_shown, underlying=att["underlying"], vol=att["vol"], rate=att["rate"],
                   time=att["time"], unexplained=(att["today"] - prev_shown) - effects,
                   prev_from="전일 기록" if recorded_prev is not None else "전일 입력으로 재계산")
        if row["status"] == "평가":
            g1 = model.greeks(p, x1, e1, st1["ki_hit"], z)
            g0 = model.greeks(p, x0, e0, st0["ki_hit"], z)
            pred = model.greek_predict(g0, p, x0, x1, e1 - e0)
            row["greeks"] = {"delta_k": g1["delta"][0], "delta_h": g1["delta"][1], "gamma_k": g1["gamma"][0], "gamma_h": g1["gamma"][1],
                             "cross": g1["cross"], "vega_k": g1["vega"][0], "vega_h": g1["vega"][1], "rho": g1["rho"], "theta": g1["theta"],
                             "delta_k_pct": g1["delta"][0] * pf[0] * 0.01, "delta_h_pct": g1["delta"][1] * pf[1] * 0.01}
            row["predict"] = pred
            v_alt = model.value(p, x1, e1, st1["ki_hit"], z_alt)[0]
            row["crn"] = {"same": att["today"] - att["prev"], "indep": v_alt - att["prev"], "noise": v_alt - att["today"]}
        # 줄 점검 표시
        flags = []
        if row["status"].endswith("조기상환"):
            flags.append(row["status"])
        if st1["ki_hit"]:
            flags.append("낙인 발생")
        elif w1 - p.ki < model.NEAR_KI:
            flags.append("낙인 근접")
        if row["next_obs"] and row["next_obs"]["bdays"] <= 5 and not row["status"].endswith("조기상환"):
            flags.append("평가일 임박")
        if abs(row["unexplained"]) > 0.005:
            flags.append("재현 차이")
        if row.get("se", 0) > 0.5:
            flags.append("잡음 큼")
        row["flags"] = flags or ["통과"]
        res[p.id] = row
    entry["products"] = res

    # ── 3. 점검 9개 ──────────────────────────────────────────────────────────
    C = []

    def chk(no, item, crit, val, ok, origin):
        C.append({"no": no, "item": item, "criterion": crit, "value": val, "pass": bool(ok), "origin": origin})

    chk(1, "입력 수집", "5개 입력을 모두 받음", f"{len(LABELS) - len(missing)}/{len(LABELS)}", not missing, "새로 추가")
    hk = ql.HongKong(ql.HongKong.HKEx)
    h_ok = q["HSCEI 종가"].asof == today.isoformat() or not hk.isBusinessDay(qt)
    k_ok = cross.asof == today.isoformat()
    r_ok = q["국고채 3년"].asof >= prev_date
    chk(2, "기준일", "코스피200 = 평가일(야후 시각으로 확인), HSCEI = 평가일(홍콩 휴장 예외), 국고3년 ≥ 전 영업일",
        f"코스피200 {cross.asof} / HSCEI {q['HSCEI 종가'].asof} / 국고3년 {q['국고채 3년'].asof}", k_ok and h_ok and r_ok, "새로 추가")
    rng_ok = x1.k200 > 0 and x1.hscei > 0 and 5 <= x1.vkospi <= 150 and 5 <= x1.vol_h <= 150 and 0 <= x1.ktb3y <= 15
    chk(3, "값 범위", "지수 > 0, VKOSPI·HSCEI 변동성 5~150, 국고3년 0~15%",
        f"VKOSPI {x1.vkospi:.2f}, HSCEI 변동성 {x1.vol_h:.2f}, 국고3년 {x1.ktb3y:.3f}%", rng_ok, "새로 추가")
    dk, dh = x1.k200 / x0.k200 - 1, x1.hscei / x0.hscei - 1
    dvk, dr = x1.vkospi - x0.vkospi, (x1.ktb3y - x0.ktb3y) * 100
    chk(4, "하루 변동 한도(이상치)", "|코스피200|·|HSCEI| ≤ 10%, |VKOSPI| ≤ 20pt, |국고3년| ≤ 30bp",
        f"{dk:+.2%} / {dh:+.2%} / {dvk:+.2f}pt / {dr:+.1f}bp", abs(dk) <= 0.10 and abs(dh) <= 0.10 and abs(dvk) <= 20 and abs(dr) <= 30, "새로 추가")
    diff = abs(cross.value / x1.k200 - 1) if cross.value else float("nan")
    chk(5, "두 출처 교차 확인", "KRX 코스피200과 야후 ^KS200 차이 ≤ 0.05%",
        f"KRX {x1.k200:,.2f} / 야후 {cross.value if cross.value else '없음'} ({diff:.4%})" if cross.value else "야후 값 없음",
        cross.value is not None and diff <= 0.0005, "새로 추가")
    mkt = x1.market()
    T_steps = n_max
    paths = model.els.simulate((1.0, 1.0), mkt, z[:, :T_steps, :], 1 / 252)
    T = T_steps / 252
    gen = []
    for i in range(2):
        pay = np.maximum(paths[:, -1, i] - 1.0, 0.0) * np.exp(-mkt.r * T)
        half = len(pay) // 2
        se = (0.5 * (pay[:half] + pay[half:])).std(ddof=1) / np.sqrt(half)
        proc = ql.BlackScholesMertonProcess(
            ql.QuoteHandle(ql.SimpleQuote(1.0)),
            ql.YieldTermStructureHandle(ql.FlatForward(qt, mkt.q[i] + mkt.quanto[i], ql.Actual365Fixed(), ql.Continuous)),
            ql.YieldTermStructureHandle(ql.FlatForward(qt, mkt.r, ql.Actual365Fixed(), ql.Continuous)),
            ql.BlackVolTermStructureHandle(ql.BlackConstantVol(qt, ql.NullCalendar(), mkt.vol[i], ql.Actual365Fixed())))
        opt = ql.EuropeanOption(ql.PlainVanillaPayoff(ql.Option.Call, 1.0), ql.EuropeanExercise(qt + ql.Period(int(round(T * 365)), ql.Days)))
        opt.setPricingEngine(ql.AnalyticEuropeanEngine(proc))
        gen.append((pay.mean(), opt.NPV(), se))
    chk(6, "경로 생성기 = QuantLib 해석해", f"{T:.2f}년 ATM 콜, 두 지수 모두 |MC − 해석해| ≤ 표준오차 × 3",
        " / ".join(f"{m:.4f} vs {e:.4f} (±{3 * s:.4f})" for m, e, s in gen), all(abs(m - e) <= 3 * s for m, e, s in gen), "ELS 헤지 프로젝트 7절")
    lr = np.diff(np.log(paths), axis=1)
    corr = float(np.corrcoef(lr[..., 0].ravel(), lr[..., 1].ravel())[0, 1])
    chk(7, "경로 상관계수", f"두 지수 하루 수익률 상관 = {model.RHO} ± 0.01", f"{corr:.4f}", abs(corr - model.RHO) <= 0.01, "ELS 헤지 프로젝트 7절")
    live = [r for r in res.values() if r["value"] is not None and r["status"] == "평가"]
    caps = {p.id: p.notional * (1 + p.coupon * p.years) for p in products}
    ok8 = all(0 < r["value"] < caps[i] and r["se"] <= 0.5 for i, r in res.items() if r["value"] is not None and r["status"] == "평가")
    chk(8, "가격 범위 · MC 표준오차", "0 < 가치 < 원금 × (1 + 쿠폰 × 3년), 표준오차 ≤ 0.5",
        f"가치 {min(r['value'] for r in live):.2f}~{max(r['value'] for r in live):.2f}, 표준오차 최대 {max(r['se'] for r in live):.3f}" if live else "평가 상품 없음",
        ok8, "ELS 헤지 프로젝트 7절(범위) + 표준오차 추가")
    sum_err = max(abs((r["value"] - r["prev_recalc"]) - (r["underlying"] + r["vol"] + r["rate"] + r["time"])) for r in live) if live else 0.0
    rep_err = max(abs(r["prev_recalc"] - r["prev_value"]) for r in live) if live else 0.0
    chk(9, "변동 분해 합 · 재현성", "분해 합 = 실제 변동(오차 < 1e-9), 전일 입력 재계산 = 전일 기록(차이 < 0.005)",
        f"분해 오차 {sum_err:.1e}, 재현 차이 {rep_err:.4f}" + ("" if prev_run else " (첫 실행: 전일 기록 없음)"),
        sum_err < 1e-9 and rep_err < 0.005, "새로 추가")
    entry["checks"] = C
    entry["pass_count"] = sum(c["pass"] for c in C)
    entry["status"] = "완료"
    entry["seconds"] = round(time.time() - t_start, 1)
    history.append(entry)
    history.sort(key=lambda h: h["date"])
    save_history(history)
    report.write_all(history, cal)
    print(f"{today} 완료: 점검 {entry['pass_count']}/9, {entry['seconds']}초")
    for i, r in res.items():
        if r["value"] is not None:
            print(f"  {i} {r['value']:.2f} (전일 {r['prev_value']:.2f}, {r['change']:+.2f} = 기초 {r['underlying']:+.2f} 변동성 {r['vol']:+.2f} "
                  f"금리 {r['rate']:+.2f} 시간 {r['time']:+.2f} 잔여 {r['unexplained']:+.3f}) {','.join(r['flags'])}")
        else:
            print(f"  {i} {r['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
