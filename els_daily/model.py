"""상품 조건, 달력, 시장 입력 → els 엔진 호출, 변동 분해, 그릭스.

엔진(els.py)은 quantlib-st-portfolio 저장소의 notebooks/els.py(커밋 ae8b412)를 그대로 복사해 쓴다.
여기서는 실제 발행일·평가일을 한국거래소 영업일 수로 바꿔 엔진에 넘기는 부분만 더한다.

공통 난수 정렬: 난수 배열 Z의 열 j는 '발행 후 j+1번째 영업일'의 충격이다. 평가일에 이미 e영업일이 지났으면
Z[:, e:]를 넘긴다. 그래서 어제(e)와 오늘(e+1) 평가는 내일 이후 같은 날짜에 같은 충격을 쓰고,
두 값의 차이에는 몬테카를로 잡음이 거의 남지 않는다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

import numpy as np
import QuantLib as ql

from . import els

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"

# ── 고정 가정(가정 시트에 그대로 나간다) ──────────────────────────────────────────
SEED = 20261110            # 원 노트북과 같은 시드
SEED_ALT = 20261111        # '다른 난수였다면' 비교용
N_PAIRS = 5000             # antithetic 5,000쌍 = 경로 10,000개
DIV = (0.015, 0.035)       # 배당수익률(코스피200, HSCEI) [예시]
RHO = 0.6                  # 두 지수 상관 [예시]
RHO_H_FX, VOL_FX = 0.3, 0.08   # 퀀토: HSCEI–원/홍콩달러 상관, 환율 변동성 [예시]
NEAR_KI = 0.10             # 가장 많이 빠진 지수가 낙인보다 10%p 안쪽이면 '낙인 근접'


# ── 달력 ────────────────────────────────────────────────────────────────────────
def krx_calendar() -> ql.Calendar:
    """한국거래소가 발표한 휴장일(state/krx_holidays.json)을 쓰고, 발표 전인 해는 QuantLib SouthKorea(KRX) 휴일로 채운다."""
    hol = json.loads((STATE / "krx_holidays.json").read_text(encoding="utf-8"))
    cal = ql.BespokeCalendar("KRX(발표 휴장일)")
    cal.addWeekend(ql.Saturday)
    cal.addWeekend(ql.Sunday)
    known = {int(y) for y in hol}
    for days in hol.values():
        for s in days:
            y, m, d = map(int, s.split("-"))
            cal.addHoliday(ql.Date(d, m, y))
    qk = ql.SouthKorea(ql.SouthKorea.KRX)
    for y in range(2026, 2031):
        if y in known:
            continue
        d = ql.Date(1, 1, y)
        while d.year() == y:
            if d.weekday() not in (ql.Saturday, ql.Sunday) and not qk.isBusinessDay(d):
                cal.addHoliday(d)
            d += 1
    return cal


def qd(s: str | date) -> ql.Date:
    if isinstance(s, date):
        return ql.Date(s.day, s.month, s.year)
    y, m, d = map(int, s.split("-"))
    return ql.Date(d, m, y)


def iso(d: ql.Date) -> str:
    return f"{d.year():04d}-{d.month():02d}-{d.dayOfMonth():02d}"


def bdays(cal: ql.Calendar, a: ql.Date, b: ql.Date) -> int:
    """a 다음 영업일부터 b까지(포함) 영업일 수 = 발행 후 지난 영업일 수."""
    return cal.businessDaysBetween(a, b, False, True)


# ── 상품 ────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Product:
    """els.pv_paths가 읽는 속성(obs_steps, n_steps 등)을 실제 날짜에서 계산해 둔 스텝다운 ELS."""
    id: str
    issue: str
    initial: tuple             # (코스피200, HSCEI) 최초 기준가
    barriers: tuple
    coupon: float
    ki: float
    obs_dates: tuple           # 평가일(YYYY-MM-DD)
    obs_steps: tuple           # 발행 후 영업일 수
    n_steps: int
    notional: float = 100.0
    years: float = 3.0
    obs_per_year: int = 2
    steps_per_year: int = 252
    note: str = ""

    @staticmethod
    def build(spec: dict, cal: ql.Calendar) -> "Product":
        issue = qd(spec["issue"])
        obs = [cal.adjust(issue + ql.Period(6 * (k + 1), ql.Months), ql.Following) for k in range(len(spec["barriers"]))]
        steps = tuple(bdays(cal, issue, d) for d in obs)
        return Product(id=spec["id"], issue=spec["issue"], initial=tuple(spec["initial"]), barriers=tuple(spec["barriers"]),
                       coupon=spec["coupon"], ki=spec["ki"], obs_dates=tuple(iso(d) for d in obs), obs_steps=steps,
                       n_steps=steps[-1], note=spec.get("note", ""))

    def terms(self) -> str:
        b = "-".join(f"{x * 100:.0f}" for x in self.barriers)
        return f"3년 · 6개월마다 {b} · 연 {self.coupon:.1%} · 낙인 {self.ki:.0%}"


def load_products(cal: ql.Calendar) -> list[Product]:
    specs = json.loads((STATE / "products.json").read_text(encoding="utf-8"))["products"]
    return [Product.build(s, cal) for s in specs]


# ── 시장 입력 → 엔진 Market ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class Inputs:
    k200: float
    hscei: float
    vkospi: float      # %
    vol_h: float       # %
    ktb3y: float       # %, 반기 복리

    def r_cont(self) -> float:
        return ql.InterestRate(self.ktb3y / 100, ql.Actual365Fixed(), ql.Compounded, ql.Semiannual).equivalentRate(
            ql.Continuous, ql.Annual, 3.0).rate()

    def market(self) -> els.Market:
        vk, vh = self.vkospi / 100, self.vol_h / 100
        return els.Market(r=self.r_cont(), q=DIV, vol=(vk, vh), rho=RHO, quanto=(0.0, RHO_H_FX * vh * VOL_FX))


def perf(p: Product, x: Inputs) -> tuple[float, float]:
    return (x.k200 / p.initial[0], x.hscei / p.initial[1])


def normals(n_steps: int, seed: int = SEED) -> np.ndarray:
    return els.normals(N_PAIRS, n_steps, seed)


def value(p: Product, x: Inputs, elapsed: int, ki_hit: bool, z: np.ndarray, perf0=None) -> tuple[float, float]:
    """(가치, 표준오차). z는 발행일 기준으로 정렬된 전체 난수, 여기서 이미 지난 날짜만큼 잘라 넘긴다."""
    perf0 = perf(p, x) if perf0 is None else perf0
    return els.price(p, x.market(), perf0, elapsed, ki_hit, z[:, elapsed:, :])


def greeks(p: Product, x: Inputs, elapsed: int, ki_hit: bool, z: np.ndarray) -> dict:
    g = els.greeks(p, x.market(), perf(p, x), elapsed, ki_hit, z[:, elapsed:, :])
    # theta는 하루 뒤 평가도 같은 날짜에 같은 충격을 쓰도록 정렬해서 다시 계산
    g["theta"] = (value(p, x, elapsed + 1, ki_hit, z)[0] - g["value"]) if elapsed + 1 < p.n_steps else 0.0
    return g


def greek_predict(g: dict, p: Product, x0: Inputs, x1: Inputs, days: int) -> dict:
    """어제 그릭스로 예측한 오늘 변동(테일러 근사). 실제 변동(전체 재평가)과 비교용."""
    p0, p1 = perf(p, x0), perf(p, x1)
    dp = (p1[0] - p0[0], p1[1] - p0[1])
    m0, m1 = x0.market(), x1.market()
    parts = {
        "delta": g["delta"][0] * dp[0] + g["delta"][1] * dp[1],
        "gamma": 0.5 * g["gamma"][0] * dp[0] ** 2 + 0.5 * g["gamma"][1] * dp[1] ** 2 + g["cross"] * dp[0] * dp[1],
        "vega": sum(g["vega"][i] * (m1.vol[i] - m0.vol[i]) / 0.01 for i in range(2)),
        "rho": g["rho"] * (m1.r - m0.r) / 1e-4,
        "theta": g["theta"] * days,
    }
    parts["total"] = sum(parts.values())
    return parts


def attribution(p: Product, x0: Inputs, e0: int, ki0: bool, x1: Inputs, e1: int, ki1: bool, z: np.ndarray) -> dict:
    """입력을 하나씩 오늘 값으로 바꿔 가며 다시 평가(같은 난수). 순서: 기초자산(+낙인 상태) → 변동성 → 금리 → 시간."""
    v0, se0 = value(p, x0, e0, ki0, z)
    xa = replace(x0, k200=x1.k200, hscei=x1.hscei)
    va = value(p, xa, e0, ki1, z)[0]
    xb = replace(xa, vkospi=x1.vkospi, vol_h=x1.vol_h)
    vb = value(p, xb, e0, ki1, z)[0]
    xc = replace(xb, ktb3y=x1.ktb3y)
    vc = value(p, xc, e0, ki1, z)[0]
    v1, se1 = value(p, x1, e1, ki1, z)
    return dict(prev=v0, prev_se=se0, today=v1, today_se=se1,
                underlying=va - v0, vol=vb - va, rate=vc - vb, time=v1 - vc)


def redemption_amount(p: Product, k: int) -> float:
    return p.notional * (1 + p.coupon * (k + 1) / p.obs_per_year)
