"""실행 기록(history) → 날짜별 엑셀 파일, latest.json, 웹 화면(docs/index.html)."""
from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import model

DOCS = model.ROOT / "docs"
REPO_URL = "https://github.com/gkfla2020-bit/els-daily-reval"

ASSUMPTIONS = [
    ("모형", "위험중립 상관 기하 브라운 운동, 하루 1단계(연 252단계), 경로 10,000개(antithetic 5,000쌍). 엔진은 ELS 헤지 프로젝트(quantlib-st-portfolio, notebooks/els.py) 그대로."),
    ("낙인", "두 지수 중 더 많이 빠진 쪽(worst-of)이 매일 종가로 낙인 아래로 내려가면 낙인 발생. 발행 후 상태는 매일 기록에 이어 붙인다."),
    ("달력", "한국거래소가 발표한 휴장일(2026~2028)로 영업일을 센다. 발행 후 지난 영업일 수로 평가 시점을 정한다."),
    ("변동성(코스피200)", "[가정] VKOSPI(30일 내재변동성)를 만기 전체에 그대로 쓴다. 실무에서는 만기별·행사가별 변동성 곡면을 쓴다."),
    ("변동성(HSCEI)", "[가정] HSCEI 내재변동성 무료 시계열이 없어 최근 60영업일 실현변동성으로 대신한다(Yahoo Finance ^HSCE 종가로 계산)."),
    ("금리", "[근사] 국고채 3년(한국은행 ECOS, 반기 복리)을 연속 복리로 바꿔 모든 만기에 쓴다. 발행사 조달 금리 대신."),
    ("배당·상관·퀀토", f"[예시] 배당 코스피200 {model.DIV[0]:.1%}, HSCEI {model.DIV[1]:.1%} / 두 지수 상관 {model.RHO} / HSCEI–환율 상관 {model.RHO_H_FX}, 환율 변동성 {model.VOL_FX:.0%}."),
    ("상품", "[예시] 조건은 국내에서 흔한 스텝다운 구조를 본뜬 것이고, 최초 기준가만 발행일의 실제 종가다. 실제 판매 상품이 아니다."),
    ("변동 분해", "어제 입력에서 시작해 기초자산(낙인 상태 포함) → 변동성 → 금리 → 시간 순서로 하나씩 오늘 값으로 바꿔 다시 평가한다. 순서를 바꾸면 교차 효과가 다른 칸으로 옮겨 간다."),
    ("공통 난수", "모든 재평가에 같은 난수를 쓰고, 난수 열을 '발행 후 몇 번째 영업일'에 맞춰 정렬한다. 그래서 어제와 오늘 값의 차이는 MC 잡음이 아니라 입력 변화의 효과다. '공통 난수' 시트에 다른 난수를 썼을 때의 차이를 같이 보인다."),
    ("설명 안 된 부분", "(오늘 값 − 전일 기록 값) − 네 효과의 합. 같은 코드·같은 난수면 0이어야 하므로, 0이 아니면 입력 수정·코드 변경·상품 변경을 의심한다."),
    ("데이터", "원자료 시계열은 저장소에 넣지 않는다. 평가에 쓴 값과 출처·받은 시각만 남긴다. 투자 권유나 실제 상품 가격이 아니다."),
]

GREEN, ORANGE, RED, GRAY, HEAD = "E6F4EA", "FFF1D6", "FDE2E1", "F3F4F6", "0A2540"


def _f(x, nd=2, sign=False):
    if x is None:
        return "–"
    return f"{x:+,.{nd}f}" if sign else f"{x:,.{nd}f}"


# ── 표 데이터(엑셀·웹 공통) ─────────────────────────────────────────────────────
def tables(e: dict, history: list[dict]) -> dict[str, dict]:
    P = e.get("products", {})
    specs = {s["id"]: s for s in json.loads((model.STATE / "products.json").read_text(encoding="utf-8"))["products"]}
    T = {}
    T["결과"] = dict(
        cols=["종목", "오늘 값", "전일 값", "변동", "기초자산 효과", "변동성 효과", "금리 효과", "시간 효과", "설명 안 된 부분", "MC 표준오차", "점검"],
        num=[1, 2, 3, 4, 5, 6, 7, 8, 9], sign=[3, 4, 5, 6, 7, 8],
        rows=[[i, r.get("value"), r.get("prev_value"), r.get("change"), r.get("underlying"), r.get("vol"), r.get("rate"),
               r.get("time"), r.get("unexplained"), r.get("se"), " · ".join(r.get("flags", [r.get("status", "")]))]
              for i, r in P.items()])
    qs = e.get("quotes", {})
    inp_rows = []
    prev_vals = e.get("prev_inputs", {})
    keymap = {"코스피200 종가": "k200", "VKOSPI 종가": "vkospi", "HSCEI 종가": "hscei", "HSCEI 변동성(60일 실현)": "vol_h", "국고채 3년": "ktb3y"}
    for k, v in qs.items():
        pv = prev_vals.get(keymap.get(k, ""), None)
        inp_rows.append([k, v.get("value"), v.get("asof"), pv, e.get("prev_date") if pv is not None else "", v.get("source"),
                         v.get("fetched_at"), v.get("error") or v.get("note"), v.get("url")])
    if e.get("inputs"):
        x = model.Inputs(**e["inputs"])
        inp_rows.append(["원화 할인 금리(연속 복리)", x.r_cont() * 100, e["date"], model.Inputs(**e["prev_inputs"]).r_cont() * 100, e["prev_date"],
                         "국고채 3년에서 계산", "", "반기 복리 → 연속 복리, 단위 %", ""])
    T["입력"] = dict(cols=["항목", "오늘 값", "기준일", "평가에 쓴 전일 값", "전일 기준일", "출처", "받은 시각", "비고", "주소"],
                    num=[1, 3], sign=[], rows=inp_rows, note=f"전일 값: {e.get('prev_from', '')}")
    prow = []
    for i, r in P.items():
        s = specs[i]
        st = r["state"]
        nx = r.get("next_obs")
        prow.append([i, s["issue"], s["initial"][0], s["initial"][1], r["terms"], " / ".join(s["obs_dates"]),
                     r["perf"][0] * 100, r["perf"][1] * 100, r["worst"] * 100, st["worst_min"] * 100, "예" if st["ki_hit"] else "아니오",
                     f"{nx['date']} ({nx['bdays']}영업일 뒤, 조건 {nx['barrier']:.0%})" if nx else "–", r["elapsed"], s.get("note", "")])
    T["상품"] = dict(cols=["종목", "발행일", "최초 코스피200", "최초 HSCEI", "조건", "평가일", "코스피200(최초 대비 %)", "HSCEI(최초 대비 %)",
                         "더 빠진 쪽 %", "발행 후 최저 %", "낙인 발생", "다음 평가일", "발행 후 영업일", "비고"],
                    num=[2, 3, 6, 7, 8, 9], sign=[], rows=prow,
                    note="조건은 [예시], 최초 기준가는 발행일 실제 종가(코스피200: 네이버 금융 차트, HSCEI: Yahoo Finance)")
    grow = []
    for i, r in P.items():
        g, pr = r.get("greeks"), r.get("predict")
        if not g:
            continue
        actual = r["value"] - r["prev_recalc"]
        grow.append([i, g["delta_k_pct"], g["delta_h_pct"], g["gamma_k"], g["gamma_h"], g["cross"], g["vega_k"], g["vega_h"],
                     g["rho"], g["theta"], pr["total"], actual, actual - pr["total"]])
    T["그릭스"] = dict(cols=["종목", "delta 코스피200 (1%당)", "delta HSCEI (1%당)", "gamma 코스피200", "gamma HSCEI", "cross gamma",
                          "vega 코스피200 (1%p당)", "vega HSCEI (1%p당)", "rho (1bp당)", "theta (1영업일)", "전일 그릭스로 예측한 변동",
                          "실제 변동(전체 재평가)", "예측과 차이"],
                     num=list(range(1, 13)), sign=[10, 11, 12], rows=grow,
                     note="원금 100 기준. gamma는 '최초 대비 비율' 단위(±5%로 계산). 예측 = 전일 그릭스 테일러 근사, 실제 = 전체 재평가. 큰 날일수록 차이가 커진다.")
    crow = [[i, r["crn"]["same"], r["crn"]["indep"], r["crn"]["noise"], r.get("se")] for i, r in P.items() if r.get("crn")]
    T["공통 난수"] = dict(cols=["종목", "같은 난수로 계산한 변동", "오늘만 다른 난수로 계산한 변동", "차이(= MC 잡음)", "MC 표준오차"],
                       num=[1, 2, 3, 4], sign=[1, 2, 3], rows=crow,
                       note="어제와 오늘을 다른 난수로 평가하면 변동에 표준오차 크기의 잡음이 섞인다. 같은 난수를 쓰면 이 잡음이 빠진다.")
    T["점검"] = dict(cols=["번호", "항목", "기준", "실제 값", "결과", "출처"], num=[], sign=[],
                   rows=[[c["no"], c["item"], c["criterion"], c["value"], "통과" if c["pass"] else "실패", c["origin"]] for c in e.get("checks", [])])
    ids = sorted({i for h in history for i in h.get("products", {})})
    lrows = []
    for h in reversed(history):
        lrows.append([h["date"], h["run_at"], h["where"] + (" (시험)" if h.get("trial") else ""), h["status"],
                      f"{h.get('pass_count', 0)}/9" if h.get("checks") else "–", h.get("seconds")]
                     + [h.get("products", {}).get(i, {}).get("value") for i in ids] + [h.get("run_url", "")])
    T["실행 로그"] = dict(cols=["평가일", "실행 시각", "실행 위치", "상태", "점검", "소요(초)"] + [f"{i} 값" for i in ids] + ["실행 기록"],
                      num=[5] + list(range(6, 6 + len(ids))), sign=[], rows=lrows)
    T["가정"] = dict(cols=["항목", "내용"], num=[], sign=[], rows=[list(a) for a in ASSUMPTIONS])
    return T


# ── 엑셀 ───────────────────────────────────────────────────────────────────────
def write_xlsx(e: dict, history: list[dict], path: Path) -> None:
    T = tables(e, history)
    wb = Workbook()
    wb.remove(wb.active)
    thin = Side(style="thin", color="D0D7DE")
    order = ["결과", "입력", "상품", "그릭스", "공통 난수", "점검", "실행 로그", "가정"]
    for name in order:
        t = T[name]
        ws = wb.create_sheet(name)
        ws["A1"] = f"ELS 일일 재평가 — {name}"
        ws["A1"].font = Font(bold=True, size=13, color=HEAD)
        ws["A2"] = (f"평가일 {e['date']} · 실행 {e['run_at']} ({e['where']}) · 점검 {e.get('pass_count', 0)}/9 · "
                    + (t.get("note") or "단위: 원금 100 기준"))
        ws["A2"].font = Font(size=9, color="6B7280")
        hr = 4
        for j, c in enumerate(t["cols"], 1):
            cell = ws.cell(hr, j, c)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=HEAD)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for i, row in enumerate(t["rows"], hr + 1):
            for j, v in enumerate(row, 1):
                cell = ws.cell(i, j, v)
                cell.border = Border(top=thin, bottom=thin, left=thin, right=thin)
                if (j - 1) in t["num"] and isinstance(v, (int, float)):
                    cell.number_format = "+#,##0.00;-#,##0.00;0.00" if (j - 1) in t["sign"] else "#,##0.00"
                    if name in ("그릭스", "공통 난수") or (name == "결과" and j - 1 == 9):
                        cell.number_format = ("+0.0000;-0.0000;0.0000" if (j - 1) in t["sign"] else "0.0000")
                if isinstance(v, str) and v.startswith("http"):
                    cell.hyperlink = v
                    cell.font = Font(color="2563EB", underline="single")
        last = hr + max(len(t["rows"]), 1)
        ws.freeze_panes = ws.cell(hr + 1, 2)
        for j, c in enumerate(t["cols"], 1):
            width = max([len(str(c))] + [len(f"{r[j - 1]:,.2f}" if isinstance(r[j - 1], float) else str(r[j - 1] or "")) for r in t["rows"]])
            ws.column_dimensions[get_column_letter(j)].width = min(max(10, width * 1.3 + 2), 70 if name == "가정" else 42)
        ws.row_dimensions[hr].height = 32
        rng = lambda col: f"{col}{hr + 1}:{col}{last}"  # noqa: E731
        if name == "결과":
            for col in "DEFGHI":
                ws.conditional_formatting.add(rng(col), CellIsRule(operator="lessThan", formula=["0"], font=Font(color="C62828")))
                ws.conditional_formatting.add(rng(col), CellIsRule(operator="greaterThan", formula=["0"], font=Font(color="1B7F3B")))
            ws.conditional_formatting.add(rng("D"), FormulaRule(formula=[f"ABS(D{hr + 1})>=1"], fill=PatternFill("solid", fgColor="FFF7C2")))
            ws.conditional_formatting.add(rng("I"), FormulaRule(formula=[f"ABS(I{hr + 1})>0.005"], fill=PatternFill("solid", fgColor=RED)))
            ws.conditional_formatting.add(rng("J"), CellIsRule(operator="greaterThan", formula=["0.5"], fill=PatternFill("solid", fgColor=RED)))
            K = rng("K")
            ws.conditional_formatting.add(K, FormulaRule(formula=[f'ISNUMBER(SEARCH("낙인 발생",K{hr + 1}))'], fill=PatternFill("solid", fgColor=RED)))
            ws.conditional_formatting.add(K, FormulaRule(formula=[f'ISNUMBER(SEARCH("근접",K{hr + 1}))'], fill=PatternFill("solid", fgColor=ORANGE)))
            ws.conditional_formatting.add(K, FormulaRule(formula=[f'K{hr + 1}="통과"'], fill=PatternFill("solid", fgColor=GREEN)))
        if name == "점검":
            ws.conditional_formatting.add(rng("E"), CellIsRule(operator="equal", formula=['"통과"'], fill=PatternFill("solid", fgColor=GREEN)))
            ws.conditional_formatting.add(rng("E"), CellIsRule(operator="equal", formula=['"실패"'], fill=PatternFill("solid", fgColor=RED)))
        if name == "상품":
            ws.conditional_formatting.add(rng("K"), CellIsRule(operator="equal", formula=['"예"'], fill=PatternFill("solid", fgColor=RED)))
        if name == "입력":
            ws.conditional_formatting.add(f"A{hr + 1}:I{last}", FormulaRule(formula=[f'ISBLANK($B{hr + 1})'], fill=PatternFill("solid", fgColor=RED)))
        if name == "가정":
            for i in range(hr + 1, last + 1):
                ws.cell(i, 2).alignment = Alignment(wrap_text=True, vertical="top")
                ws.cell(i, 1).alignment = Alignment(vertical="top")
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


# ── 웹 화면 ─────────────────────────────────────────────────────────────────────
def _cell(v, j, t) -> str:
    cls = []
    if isinstance(v, (int, float)) and not isinstance(v, bool) and j in t["num"]:
        nd = 4 if t.get("_nd4") and j in t["_nd4"] else 2
        if round(v, nd) == 0:
            v = 0.0                                   # -0.00 대신 0.00
        s = _f(v, nd, j in t["sign"])
        cls.append("n")
        if j in t["sign"]:
            cls.append("neg" if v < -1e-12 else "pos" if v > 1e-12 else "")
    elif v is None:
        s = "–"
    else:
        s = str(v)
    if isinstance(v, str) and v.startswith("http"):
        return f'<td><a href="{html.escape(v)}" target="_blank" rel="noopener">열기</a></td>'
    if isinstance(v, str):
        if v == "통과":
            cls.append("ok")
        elif v == "실패" or "낙인 발생" in v or v == "예":
            cls.append("bad")
        elif "근접" in v or "임박" in v or "재현 차이" in v:
            cls.append("warn")
    return f'<td class="{" ".join(c for c in cls if c)}">{html.escape(s)}</td>'


def write_html(history: list[dict], runs: list[dict]) -> None:
    e = next(h for h in reversed(history) if h["status"] == "완료")
    T = tables(e, history)
    T["그릭스"]["_nd4"] = set(range(1, 13))
    T["공통 난수"]["_nd4"] = {1, 2, 3, 4}
    T["결과"]["_nd4"] = {9}
    tabs, panes = [], []
    for k, (name, t) in enumerate(T.items()):
        tabs.append(f'<button class="tab{" on" if k == 0 else ""}" data-t="p{k}">{html.escape(name)}</button>')
        head = "".join(f"<th>{html.escape(c)}</th>" for c in t["cols"])
        body = "".join("<tr>" + "".join(_cell(v, j, t) for j, v in enumerate(r)) + "</tr>" for r in t["rows"])
        note = f'<p class="note">{html.escape(t["note"])}</p>' if t.get("note") else ""
        panes.append(f'<div class="pane{" on" if k == 0 else ""}" id="p{k}">{note}<div class="grid"><table><thead><tr><th class="rn"></th>{head}</tr></thead>'
                     f'<tbody>{body.replace("<tr>", "<tr><td class=rn></td>")}</tbody></table></div></div>')
    archive = "".join(f'<tr><td>{r["date"]}</td><td>{html.escape(r["where"])}</td><td class="{"ok" if r["pass"] == 9 else "bad"}">{r["pass"]}/9</td>'
                      f'<td><a href="runs/{r["date"]}/{r["file"]}">엑셀</a></td><td>{("<a href=" + chr(34) + r["url"] + chr(34) + " target=_blank rel=noopener>Actions 기록</a>") if r["url"] else "–"}</td></tr>'
                      for r in reversed(runs))
    fails = [c for c in e["checks"] if not c["pass"]]
    n_days = len(runs)
    latest_file = f"runs/{e['date']}/{runs[-1]['file']}"
    page = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light">
<title>ELS 일일 재평가 시트 — 정하림</title>
<meta name="description" content="코스피200·HSCEI 스텝다운 ELS를 매일 장 마감 뒤 다시 평가하고, 전일 대비 변동을 기초자산·변동성·금리·시간 효과로 나누는 자동 평가 시트">
<style>
:root{{color-scheme:light;--bg:#f6f8fa;--fg:#0a2540;--mut:#6b7280;--line:#d0d7de;--head:#0a2540;--card:#fff;--acc:#2563eb}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic",sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:28px 18px 60px}}a{{color:var(--acc)}}
h1{{font-size:1.55rem;margin:0 0 6px}}.sub{{color:var(--mut);margin:0 0 18px}}
.kpis{{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 18px}}.kpi{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:150px}}
.kpi b{{display:block;font-size:1.25rem}}.kpi span{{color:var(--mut);font-size:.8rem}}
.btns a{{display:inline-block;margin:0 8px 8px 0;padding:8px 14px;border-radius:6px;border:1px solid var(--line);background:var(--card);text-decoration:none;color:var(--fg);font-weight:600}}
.btns a.pri{{background:#1d6f42;border-color:#1d6f42;color:#fff}}
.book{{background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}}
.tabs{{display:flex;flex-wrap:wrap;border-top:1px solid var(--line);background:var(--bg)}}
.tab{{border:0;border-right:1px solid var(--line);background:transparent;color:var(--fg);padding:8px 16px;cursor:pointer;font:inherit}}
.tab.on{{background:var(--card);font-weight:700;box-shadow:inset 0 2px 0 #1d6f42}}
.pane{{display:none}}.pane.on{{display:block}}.note{{margin:0;padding:8px 12px;color:var(--mut);font-size:.82rem;border-bottom:1px solid var(--line)}}
.grid{{overflow:auto;max-height:560px}}table{{border-collapse:collapse;width:100%;font-size:.86rem}}
th,td{{border:1px solid var(--line);padding:5px 9px;white-space:nowrap;text-align:left}}
th{{position:sticky;top:0;background:var(--head);color:#fff;font-weight:600;z-index:1}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}td.neg{{color:#c62828}}td.pos{{color:#1b7f3b}}
td.ok{{background:#e6f4ea;color:#14532d}}td.bad{{background:#fde2e1;color:#7f1d1d}}td.warn{{background:#fff1d6;color:#7c4a03}}
.rn{{background:var(--bg);color:var(--mut);width:28px;padding:0;text-align:center}}
tbody{{counter-reset:r}}tbody tr{{counter-increment:r}}tbody td.rn::before{{content:counter(r)}}
#p7 td:nth-child(3){{white-space:normal;min-width:520px}}
section{{margin-top:28px}}h2{{font-size:1.1rem;margin:0 0 10px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 18px}}
.card p{{margin:.4em 0}}.small{{font-size:.82rem;color:var(--mut)}}.alert{{background:#fde2e1;color:#7f1d1d;border-radius:6px;padding:8px 12px;margin-bottom:12px}}
code{{background:var(--bg);padding:1px 5px;border-radius:4px}}
</style></head><body><div class="wrap">
<p class="small"><a href="/">← 블로그</a></p>
<h1>ELS 일일 재평가 시트</h1>
<p class="sub">코스피200·HSCEI 스텝다운 ELS 5종을 매일 장 마감 뒤 다시 평가하고, 전일 대비 변동을 <b>기초자산·변동성·금리·시간 효과</b>로 나눕니다. GitHub Actions가 평일 18:40(KST)에 실행하고 결과를 엑셀로 남깁니다.</p>
{"".join(f'<div class="alert">점검 실패: {html.escape(c["item"])} — {html.escape(str(c["value"]))}</div>' for c in fails)}
<div class="kpis">
<div class="kpi"><span>최신 평가일</span><b>{e['date']}</b></div>
<div class="kpi"><span>점검</span><b>{e['pass_count']}/9 통과</b></div>
<div class="kpi"><span>누적 실행</span><b>{n_days}영업일</b></div>
<div class="kpi"><span>코스피200 · VKOSPI</span><b>{e['inputs']['k200']:,.2f} · {e['inputs']['vkospi']:.2f}</b></div>
<div class="kpi"><span>HSCEI · 국고 3년</span><b>{e['inputs']['hscei']:,.2f} · {e['inputs']['ktb3y']:.3f}%</b></div>
</div>
<div class="btns"><a class="pri" href="{latest_file}">최신 엑셀 받기 (.xlsx)</a><a href="{REPO_URL}" target="_blank" rel="noopener">코드 보기 (GitHub)</a><a href="{REPO_URL}/actions" target="_blank" rel="noopener">매일 실행 기록 (Actions)</a></div>
<div class="book">{"".join(panes)}<div class="tabs">{"".join(tabs)}</div></div>
<p class="small">실행 {html.escape(e['run_at'])} · {html.escape(e['where'])} · 소요 {e['seconds']}초 · 단위: 원금 100 기준. 숫자는 모두 실제 실행 결과이고, 가정은 '가정' 시트에 적었습니다. 투자 권유가 아닙니다.</p>

<section><h2>어떻게 계산하나</h2><div class="card">
<p><b>1. 입력</b> 한국거래소 지수 사이트에서 코스피200·VKOSPI 종가, 한국은행 ECOS에서 국고채 3년, Yahoo Finance에서 HSCEI 종가를 받고 출처와 받은 시각을 남깁니다.</p>
<p><b>2. 재평가</b> 두 지수 상관 기하 브라운 운동 몬테카를로(경로 10,000개, 하루 1단계)로 상품마다 오늘 가치를 다시 매깁니다. 낙인은 매일 종가로 확인해 다음 날로 이어 붙입니다.</p>
<p><b>3. 변동 분해</b> 어제 입력에서 시작해 기초자산 → 변동성 → 금리 → 시간 순서로 하나씩 오늘 값으로 바꿔 다시 평가합니다. 바꿀 때마다 생긴 차이가 그 입력의 효과입니다.</p>
<p><b>4. 같은 난수</b> 모든 재평가에 같은 난수를 쓰고, 난수를 '발행 후 몇 번째 영업일'에 맞춰 정렬합니다. 그래서 차이가 MC 잡음이 아니라 진짜 효과가 됩니다. '공통 난수' 시트에서 오늘만 다른 난수를 쓰면 변동이 얼마나 흔들리는지 매일 확인합니다.</p>
<p><b>5. 점검 9개</b> 수집·기준일·값 범위·하루 변동 한도·두 출처 교차 확인(데이터), 경로 생성기 vs QuantLib 해석해·상관계수(모형), 가격 범위·표준오차, 분해 합·재현성(결과)을 통과/실패로 찍습니다.</p>
</div></section>

<section><h2>날짜별 기록</h2><div class="card grid" style="max-height:none;padding:0"><table><thead><tr><th>평가일</th><th>실행 위치</th><th>점검</th><th>파일</th><th>실행 기록</th></tr></thead><tbody>{archive}</tbody></table></div></section>
</div>
<script>document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{{document.querySelectorAll('.tab,.pane').forEach(x=>x.classList.remove('on'));b.classList.add('on');document.getElementById(b.dataset.t).classList.add('on')}})</script>
</body></html>"""
    (DOCS / "index.html").write_text(page, encoding="utf-8")


def write_all(history: list[dict], cal) -> None:
    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").write_text("")
    runs = []
    for h in history:
        if h["status"] != "완료":
            continue
        fname = f"ELS_재평가_{h['date'].replace('-', '')}.xlsx"
        d = DOCS / "runs" / h["date"]
        if not (d / fname).exists() or h is history[-1] or h["date"] == max(x["date"] for x in history):
            write_xlsx(h, [x for x in history if x["date"] <= h["date"]], d / fname)
        runs.append({"date": h["date"], "where": h["where"] + (" (시험)" if h.get("trial") else ""), "pass": h["pass_count"],
                     "file": fname, "url": h.get("run_url", "")})
    if not runs:
        return
    e = next(h for h in reversed(history) if h["status"] == "완료")
    shutil.copyfile(DOCS / "runs" / e["date"] / runs[-1]["file"], DOCS / "latest.xlsx")
    latest = {"date": e["date"], "run_at": e["run_at"], "where": e["where"], "pass_count": e["pass_count"], "days": len(runs),
              "inputs": e["inputs"], "latest_xlsx": f"runs/{e['date']}/{runs[-1]['file']}",
              "last_status": history[-1]["status"], "last_date": history[-1]["date"],
              "products": [{"id": i, "value": r.get("value"), "prev": r.get("prev_value"), "change": r.get("change"),
                            "underlying": r.get("underlying"), "vol": r.get("vol"), "rate": r.get("rate"), "time": r.get("time"),
                            "unexplained": r.get("unexplained"), "flags": r.get("flags", [r.get("status")])} for i, r in e["products"].items()]}
    (DOCS / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")
    write_html(history, runs)
