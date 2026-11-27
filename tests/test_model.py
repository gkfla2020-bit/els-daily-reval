"""빠른 단위 점검: 변동 분해의 합, 공통 난수 정렬, 날짜 → 영업일 변환."""
from __future__ import annotations

import sys

from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from els_daily import els, model  # noqa: E402

CAL = model.krx_calendar()
SPEC = dict(id="T", issue="2026-11-02", initial=[1000.0, 8000.0], barriers=[0.9, 0.9, 0.85, 0.85, 0.8, 0.75], coupon=0.07, ki=0.5)
P = model.Product.build(SPEC, CAL)
X0 = model.Inputs(k200=980.0, hscei=7900.0, vkospi=30.0, vol_h=25.0, ktb3y=3.0)
X1 = model.Inputs(k200=1000.0, hscei=7800.0, vkospi=32.0, vol_h=24.0, ktb3y=3.05)


def small_z(seed=1):
    return els.normals(500, P.n_steps, seed)


def test_obs_steps_follow_krx_calendar():
    assert P.obs_dates[0] == "2027-05-04"              # 5/2 일요일 → 5/3 대체휴일 → 5/4
    assert P.obs_steps == tuple(model.bdays(CAL, model.qd(P.issue), model.qd(d)) for d in P.obs_dates)
    assert not CAL.isBusinessDay(model.qd("2026-06-03"))  # 지방선거일(거래소 발표 휴장일)


def test_attribution_sums_to_total_change():
    z = small_z()
    a = model.attribution(P, X0, 10, False, X1, 11, False, z)
    assert abs((a["today"] - a["prev"]) - (a["underlying"] + a["vol"] + a["rate"] + a["time"])) < 1e-12


def test_same_inputs_same_value():
    z = small_z()
    assert model.value(P, X1, 11, False, z) == model.value(P, X1, 11, False, z)


def test_time_shift_reuses_shocks_by_date():
    """하루 지난 평가는 '내일 이후' 충격을 그대로 다시 쓴다: 오늘 경로 = 어제 경로를 하루 미룬 것."""
    z = small_z()
    m = X1.market()
    yday = els.simulate((1.0, 1.0), m, z[:, 10:P.n_steps, :], 1 / 252)
    today = els.simulate((1.0, 1.0), m, z[:, 11:P.n_steps, :], 1 / 252)
    step1 = yday[:, :1, :]                                  # 어제 기준 '첫날' 움직임
    np.testing.assert_allclose(today, yday[:, 1:, :] / step1, rtol=1e-6)


def test_common_numbers_reduce_noise():
    """시드를 바꿔 가며 '하루 변동'을 구하면, 같은 난수 쪽이 다른 난수 쪽보다 훨씬 덜 흔들린다."""
    same, indep = [], []
    for s in range(1, 9):
        z, z2 = small_z(s), small_z(100 + s)
        v0 = model.value(P, X0, 10, False, z)[0]
        same.append(model.value(P, X1, 11, False, z)[0] - v0)
        indep.append(model.value(P, X1, 11, False, z2)[0] - v0)
    assert np.std(same) < 0.5 * np.std(indep)
