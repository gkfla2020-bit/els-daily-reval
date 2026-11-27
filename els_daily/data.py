"""시장데이터 수집. 공식 출처를 먼저 쓰고, 받은 주소·시각·기준일을 같이 남긴다.

- 코스피200, VKOSPI: 한국거래소 지수 사이트(index.krx.co.kr) '주가지수' 화면 조회
- 코스피200 교차 확인용: Yahoo Finance ^KS200
- HSCEI 종가와 실현변동성: Yahoo Finance ^HSCE (무료 공식 시계열이 없어 대신 씀 → 가정 시트에 적음)
- 국고채 3년: 한국은행 ECOS Open API(공개 sample 키, 10행까지)
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
HKT = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (els-daily-reval; personal study)"}

KRX_IDX_PAGE = "https://index.krx.co.kr/contents/MKD/03/0304/03040200/MKD03040200.jsp"
KRX_HOLIDAY_PAGE = "https://open.krx.co.kr/contents/MKD/01/0110/01100305/MKD01100305.jsp"
ECOS_URL = "https://ecos.bok.or.kr/api/StatisticSearch/sample/json/kr/1/10/817Y002/D/{start}/{end}/010200000"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"


def now_kst() -> datetime:
    return datetime.now(KST)


def _get(url: str, data: bytes | None = None, headers: dict | None = None, tries: int = 3) -> bytes:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read()
        except Exception as e:  # 네트워크 일시 오류는 잠깐 쉬고 다시
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url} 받기 실패: {last}")


@dataclass
class Quote:
    name: str                 # 화면에 보일 이름
    value: float | None       # 값(지수는 포인트, 금리는 %)
    asof: str | None          # 기준일 YYYY-MM-DD
    source: str
    url: str
    fetched_at: str
    note: str = ""
    prev_value: float | None = None
    prev_asof: str | None = None
    error: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def krx_indices(today: date) -> dict[str, Quote]:
    """KRX 지수 사이트에서 코스피200(대표지수 목록)과 VKOSPI(옵션지수 목록)의 현재가와 전일 대비."""
    out: dict[str, Quote] = {}
    want = {"코스피 200": ("01", "02", "코스피200 종가"), "코스피 200 변동성지수": ("02", "02", "VKOSPI 종가")}
    for idx_nm, (up, mid, label) in want.items():
        at = now_kst().strftime("%Y-%m-%d %H:%M:%S KST")
        try:
            otp = _get("https://index.krx.co.kr/contents/COM/GenerateOTP.jspx?" +
                       urllib.parse.urlencode({"bld": "/IDX/03/0304/03040100/mkd03040100", "name": "grid"}),
                       headers={"Referer": KRX_IDX_PAGE})
            params = {"idx_upclss_cd": up, "idx_midclss_cd": mid, "lang": "ko", "bz_dd": today.strftime("%Y%m%d")}
            body = _get("https://index.krx.co.kr/contents/IDX/99/IDX99000001.jspx",
                        data=urllib.parse.urlencode({**params, "code": otp.decode()}).encode(),
                        headers={"Referer": KRX_IDX_PAGE})
            rows = json.loads(body)["output"]
            row = next(r for r in rows if r["idx_nm"] == idx_nm)
            v = float(row["prsnt_prc"].replace(",", ""))
            chg = float(row["cmpprevdd"].replace(",", ""))
            sign = {"1": 1, "2": -1, "3": 0, "4": 1, "5": -1}.get(row["idx_fluc_tp_cd"], 1)
            out[label] = Quote(label, v, today.isoformat(), "한국거래소 지수 사이트", KRX_IDX_PAGE, at,
                               note=f"'{idx_nm}' 현재가(장 마감 뒤 조회 = 종가), 전일 대비 {sign * chg:+.2f}",
                               prev_value=round(v - sign * chg, 4),
                               extra={"fluc_tp_cd": row["idx_fluc_tp_cd"], "cmpprevdd": chg})
        except Exception as e:
            out[label] = Quote(label, None, None, "한국거래소 지수 사이트", KRX_IDX_PAGE, at, error=str(e))
    return out


def yahoo_series(sym: str, rng: str = "1y") -> tuple[list[tuple[str, float]], dict]:
    """(기준일, 종가) 목록과 meta. 기준일은 거래소 현지 날짜."""
    url = YAHOO_URL.format(sym=urllib.parse.quote(sym), rng=rng)
    j = json.loads(_get(url))["chart"]["result"][0]
    off = j["meta"].get("gmtoffset", 0)
    tz = timezone(timedelta(seconds=off))
    closes = j["indicators"]["quote"][0].get("close") or []
    rows = [(datetime.fromtimestamp(t, tz).date().isoformat(), float(c))
            for t, c in zip(j.get("timestamp", []), closes) if c is not None]
    return rows, j["meta"]


def hscei(today: date, window: int = 60) -> tuple[Quote, Quote]:
    """HSCEI 종가와 60영업일 실현변동성(연율, 로그수익률 표준편차 × √252). 각각 전일 값도 같이."""
    at = now_kst().strftime("%Y-%m-%d %H:%M:%S KST")
    url = YAHOO_URL.format(sym="%5EHSCE", rng="1y")
    try:
        rows, meta = yahoo_series("^HSCE", "1y")
        rows = [r for r in rows if r[0] <= today.isoformat()]
        # 홍콩 장중이면 마지막 값이 확정 종가가 아닐 수 있다 → 장 마감(16:10 HKT) 전 조회면 오늘 값은 버린다
        hk_now = datetime.now(HKT)
        if rows and rows[-1][0] == hk_now.date().isoformat() and (hk_now.hour, hk_now.minute) < (16, 20):
            rows = rows[:-1]
        import numpy as np
        px = np.array([c for _, c in rows])
        lr = np.diff(np.log(px))
        vol = float(lr[-window:].std(ddof=1) * np.sqrt(252))
        vol_prev = float(lr[-window - 1:-1].std(ddof=1) * np.sqrt(252))
        close = Quote("HSCEI 종가", rows[-1][1], rows[-1][0], "Yahoo Finance (^HSCE)", url, at,
                      note="HSCEI 무료 공식 시계열 대신 사용", prev_value=rows[-2][1], prev_asof=rows[-2][0])
        vq = Quote("HSCEI 변동성(60일 실현)", vol * 100, rows[-1][0], "Yahoo Finance (^HSCE)에서 계산", url, at,
                   note=f"최근 {window}개 로그수익률 표준편차 × √252 (내재변동성 대신 쓰는 가정)",
                   prev_value=vol_prev * 100, prev_asof=rows[-2][0])
        close.extra["rows"] = rows[-(window + 15):]      # 기준일별 값을 다시 찾을 때만 쓴다(저장하지 않음)
        return close, vq
    except Exception as e:
        bad = Quote("HSCEI 종가", None, None, "Yahoo Finance (^HSCE)", url, at, error=str(e))
        return bad, Quote("HSCEI 변동성(60일 실현)", None, None, "Yahoo Finance (^HSCE)에서 계산", url, at, error=str(e))


def yahoo_ks200(today: date) -> Quote:
    at = now_kst().strftime("%Y-%m-%d %H:%M:%S KST")
    url = YAHOO_URL.format(sym="%5EKS200", rng="5d")
    try:
        _, meta = yahoo_series("^KS200", "5d")
        d = datetime.fromtimestamp(meta["regularMarketTime"], KST).date().isoformat()
        return Quote("코스피200 (교차 확인)", float(meta["regularMarketPrice"]), d, "Yahoo Finance (^KS200)", url, at,
                     note="KRX 값과 비교만 하고 평가에는 쓰지 않음")
    except Exception as e:
        return Quote("코스피200 (교차 확인)", None, None, "Yahoo Finance (^KS200)", url, at, error=str(e))


def ktb3y(today: date) -> Quote:
    at = now_kst().strftime("%Y-%m-%d %H:%M:%S KST")
    start = (today - timedelta(days=30)).strftime("%Y%m%d")
    url = ECOS_URL.format(start=start, end=today.strftime("%Y%m%d"))
    try:
        # 공개 sample 키는 한 번에 10행까지라, 전체 행 수를 먼저 보고 마지막 10행을 받는다
        total = int(json.loads(_get(url.replace("/1/10/", "/1/1/")))["StatisticSearch"]["list_total_count"])
        url = url.replace("/1/10/", f"/{max(1, total - 9)}/{total}/")
        rows = json.loads(_get(url))["StatisticSearch"]["row"]
        rows = sorted((r["TIME"], float(r["DATA_VALUE"])) for r in rows)
        f = lambda s: f"{s[:4]}-{s[4:6]}-{s[6:]}"  # noqa: E731
        return Quote("국고채 3년", rows[-1][1], f(rows[-1][0]), "한국은행 ECOS (817Y002 / 010200000)", url, at,
                     note="시장금리(일별) 국고채(3년), 연 %, 반기 복리", prev_value=rows[-2][1], prev_asof=f(rows[-2][0]),
                     extra={"rows": [(f(d), v) for d, v in rows]})
    except Exception as e:
        return Quote("국고채 3년", None, None, "한국은행 ECOS (817Y002 / 010200000)", url, at, error=str(e))


def krx_holidays(year: int) -> list[str]:
    """한국거래소 정보데이터시스템 '휴장일' 목록(YYYY-MM-DD)."""
    bld = "MKD/01/0110/01100305/mkd01100305_01"
    otp = _get("https://open.krx.co.kr/contents/COM/GenerateOTP.jspx?" +
               urllib.parse.urlencode({"bld": bld, "name": "form"}), headers={"Referer": KRX_HOLIDAY_PAGE})
    params = {"search_bas_yy": str(year), "gridTp": "KRX", "pagePath": "/contents/MKD/01/0110/01100305/MKD01100305.jsp"}
    body = _get("https://open.krx.co.kr/contents/OPN/99/OPN99000001.jspx",
                data=urllib.parse.urlencode({**params, "code": otp.decode()}).encode(), headers={"Referer": KRX_HOLIDAY_PAGE})
    return [r["calnd_dd"] for r in json.loads(body)["block1"]]
