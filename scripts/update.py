"""
Peer 밸류에이션 계산기 — 데이터 수집
  1) 회사 목록   : KIND 상장법인목록(업종·주요제품·결산월) + DART 고유번호
  2) 주가        : KRX Open API 일별매매정보(코스피·코스닥) → 종목별 종가·거래량·거래대금·상장주식수
  3) 재무        : DART 전체 재무제표(fnlttSinglAcntAll) → 매출·영업이익·순이익(지배)·감가상각·자본·현금·차입금
결과
  data/companies.json      전 종목 목록(검색·추천용)
  data/px/<code>.json      종목별 일별 시세(최근 약 2년)
  data/fin/<code>.json     종목별 재무(보고서별 누적·전년동기·연간)
"""
import io
import json
import os
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PX = DATA / "px"
FIN = DATA / "fin"
KST = ZoneInfo("Asia/Seoul")
TODAY = datetime.now(KST).date()

KRX_KEY = os.environ.get("KRX_API_KEY", "").strip()
DART_KEY = os.environ.get("DART_API_KEY", "").strip()
KRX_API = "https://data-dbg.krx.co.kr/svc/apis/sto"
DART_API = "https://opendart.fss.or.kr/api"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

PX_DAYS = 520            # 종목별로 보관할 거래일 수(약 2년)
DART_BUDGET = int(os.environ.get("DART_BUDGET", "9000"))   # 한 번 실행에 쓸 DART 호출 수(하루 한도 2만, IPO 사이트와 나눠 씀)
TIME_LIMIT_MIN = float(os.environ.get("TIME_LIMIT_MIN", "200"))
FIN_VER = 1
START = time.time()


def log(*a):
    print(*a, flush=True)


def mins():
    return (time.time() - START) / 60


def num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


# ---------------------------------------------------------------- 1) 회사 목록
def kind_corp_list():
    """KIND 상장법인목록(코스피·코스닥): 종목코드·회사명·업종·주요제품·상장일·결산월"""
    out = {}
    for mkt, label in (("stockMkt", "코스피"), ("kosdaqMkt", "코스닥")):
        r = requests.get("https://kind.krx.co.kr/corpgeneral/corpList.do",
                         params={"method": "download", "searchType": "13", "marketType": mkt},
                         headers={"User-Agent": UA}, timeout=60)
        r.encoding = "euc-kr"
        df = pd.read_html(io.StringIO(r.text), header=0)[0]
        for _, row in df.iterrows():
            code = str(row.get("종목코드", "")).strip()
            if re.fullmatch(r"\d+", code):
                code = code.zfill(6)
            if not code or code == "nan":
                continue
            out[code] = {
                "code": code, "name": str(row.get("회사명", "")).strip(), "market": label,
                "sector": str(row.get("업종", "") or "").strip() or None,
                "product": str(row.get("주요제품", "") or "").strip() or None,
                "listed": str(row.get("상장일", "") or "")[:10] or None,
                "fye": str(row.get("결산월", "") or "").strip() or None,
            }
            for k in ("sector", "product"):
                if out[code][k] == "nan":
                    out[code][k] = None
    log(f"[회사] KIND 상장법인 {len(out)}개")
    return out


def dart_corp_map():
    r = requests.get(f"{DART_API}/corpCode.xml", params={"crtfc_key": DART_KEY}, timeout=120)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(z.read(z.namelist()[0]))
    m = {}
    for el in root.iter("list"):
        sc = (el.findtext("stock_code") or "").strip()
        if sc:
            m[sc] = el.findtext("corp_code")
    log(f"[DART] 고유번호 {len(m)}개")
    return m


# ---------------------------------------------------------------- 2) 주가(KRX)
def krx_day(d):
    """하루치 코스피·코스닥 전 종목 시세. 휴장일이면 빈 리스트"""
    rows = []
    for svc in ("stk_bydd_trd", "ksq_bydd_trd"):
        for attempt in range(3):
            try:
                r = requests.get(f"{KRX_API}/{svc}", params={"basDd": d.strftime("%Y%m%d")},
                                 headers={"AUTH_KEY": KRX_KEY}, timeout=60)
                r.raise_for_status()
                rows += r.json().get("OutBlock_1", []) or []
                break
            except Exception as e:
                if attempt == 2:
                    raise RuntimeError(f"KRX {svc} {d}: {e}")
                time.sleep(3)
        time.sleep(0.15)
    return rows


def update_prices(companies):
    if not KRX_KEY:
        log("[주가] KRX_API_KEY 가 없어 건너뜀(키 승인 후 GitHub Secrets 에 넣으면 자동 시작)")
        return
    PX.mkdir(parents=True, exist_ok=True)
    state_p = DATA / "px_state.json"
    st = json.loads(state_p.read_text("utf-8")) if state_p.exists() else {"done": []}
    done = set(st["done"])
    # 받을 날짜: 최근 PX_DAYS 거래일(주말 제외 달력일로 넉넉히), 이미 받은 날 제외
    days, d = [], TODAY - timedelta(days=1)
    while len(days) < int(PX_DAYS * 1.08) and d > TODAY - timedelta(days=900):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    todo = [x for x in days if x.isoformat() not in done]
    todo.sort(reverse=True)            # 최근 날짜부터
    log(f"[주가] 받을 날짜 {len(todo)}일")
    if not todo:
        return
    series = {}                         # code -> {date: (close, vol, val, shares, mcap)}
    got = 0
    for d in todo:
        if mins() > TIME_LIMIT_MIN * 0.45:
            log("[주가] 시간 제한 → 나머지는 다음 실행에")
            break
        rows = krx_day(d)
        done.add(d.isoformat())
        if not rows:
            continue                    # 휴장일
        got += 1
        ds = d.isoformat()
        for r in rows:
            code = (r.get("ISU_CD") or "").strip()
            c = num(r.get("TDD_CLSPRC"))
            if not code or not c:
                continue
            series.setdefault(code, {})[ds] = [c, num(r.get("ACC_TRDVOL")), num(r.get("ACC_TRDVAL")),
                                              num(r.get("LIST_SHRS")), num(r.get("MKTCAP"))]
            if code in companies and not companies[code].get("isuName"):
                companies[code]["isuName"] = r.get("ISU_NM")
    log(f"[주가] 거래일 {got}일 받음, 종목 {len(series)}개")
    # 종목별 파일에 합치기
    for code, add in series.items():
        p = PX / f"{code}.json"
        cur = {}
        if p.exists():
            try:
                j = json.loads(p.read_text("utf-8"))
                for i, ds in enumerate(j["d"]):
                    cur[ds] = [j["c"][i], j["v"][i], j["a"][i], j["s"][i], j["m"][i]]
            except Exception:
                cur = {}
        cur.update(add)
        keys = sorted(cur)[-PX_DAYS:]
        p.write_text(json.dumps({"d": keys, "c": [cur[k][0] for k in keys], "v": [cur[k][1] for k in keys],
                                 "a": [cur[k][2] for k in keys], "s": [cur[k][3] for k in keys],
                                 "m": [cur[k][4] for k in keys]}, separators=(",", ":")), "utf-8")
    keep = sorted(done)[-int(PX_DAYS * 1.2):]
    st["done"] = keep
    st["last"] = max(keep) if keep else None
    state_p.write_text(json.dumps(st, indent=0), "utf-8")


# ---------------------------------------------------------------- 3) 재무(DART)
class DartBudget:
    def __init__(self, n):
        self.left = n
        self.calls = 0

    def get(self, path, **params):
        if self.left <= 0:
            raise StopIteration("DART 호출 한도")
        params["crtfc_key"] = DART_KEY
        for attempt in range(3):
            try:
                r = requests.get(f"{DART_API}/{path}", params=params, timeout=60)
                r.raise_for_status()
                self.left -= 1
                self.calls += 1
                time.sleep(0.08)
                j = r.json()
                if j.get("status") == "020":        # 사용한도 초과
                    self.left = 0
                    raise StopIteration("DART 사용한도 초과(020)")
                return j
            except StopIteration:
                raise
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2)


REPORTS = {"11013": "1Q", "11012": "2Q", "11014": "3Q", "11011": "FY"}


def wanted_reports():
    """지금 시점에 받아 둘 보고서: 최근 사업연도 2개 + 올해·작년 분기들(LTM·연환산 계산용)"""
    y = TODAY.year
    out = [(y - 1, "11011"), (y - 2, "11011")]
    # 분기보고서 제출기한(분기말+45일) 지난 것만
    for (yy, rc, end) in ((y, "11014", date(y, 11, 14)), (y, "11012", date(y, 8, 14)), (y, "11013", date(y, 5, 15)),
                          (y - 1, "11014", date(y - 1, 11, 14)), (y - 1, "11012", date(y - 1, 8, 14))):
        if TODAY > end:
            out.append((yy, rc))
    if TODAY < date(y, 3, 31):      # 작년 사업보고서 나오기 전이면 재작년 3분기까지
        out.append((y - 2, "11014"))
    return out


def pick(rows, sj, ids=(), names=(), name_re=None):
    """계정 하나 찾기: account_id 우선, 없으면 계정명"""
    cand = [r for r in rows if r.get("sj_div") in sj]
    for i in ids:
        for r in cand:
            if r.get("account_id") == i:
                return r
    for n in names:
        for r in cand:
            if re.sub(r"\s", "", r.get("account_nm", "")) == n:
                return r
    if name_re:
        for r in cand:
            if re.search(name_re, re.sub(r"\s", "", r.get("account_nm", ""))):
                return r
    return None


def parse_fs(rows, annual):
    """전체 재무제표 행 → 핵심 숫자. 손익은 (이번 누적, 전년 동기 누적), 재무상태는 이번 기말"""
    def isv(r):
        if not r:
            return None, None
        if annual:
            return num(r.get("thstrm_amount")), num(r.get("frmtrm_amount"))
        cur = num(r.get("thstrm_add_amount"))
        prv = num(r.get("frmtrm_add_amount"))
        if cur is None:
            cur = num(r.get("thstrm_amount"))
        if prv is None:
            prv = num(r.get("frmtrm_amount"))
        return cur, prv
    IS = ("IS", "CIS")
    out = {}
    for key, kw in (
        ("rev", dict(ids=("ifrs-full_Revenue", "ifrs_Revenue"), names=("매출액", "수익(매출액)", "영업수익", "매출", "수익"), name_re=r"^(매출|영업수익)")),
        ("op", dict(ids=("dart_OperatingIncomeLoss",), names=("영업이익", "영업이익(손실)", "영업손익"), name_re=r"^영업(이익|손실|손익)")),
        ("ni", dict(ids=("ifrs-full_ProfitLoss", "ifrs_ProfitLoss"), names=("당기순이익", "당기순이익(손실)", "분기순이익", "분기순이익(손실)", "반기순이익", "반기순이익(손실)", "당기순손익"), name_re=r"^(당기|분기|반기)순(이익|손실|손익)")),
        ("niP", dict(ids=("ifrs-full_ProfitLossAttributableToOwnersOfParent", "ifrs_ProfitLossAttributableToOwnersOfParent"), names=("지배기업소유주지분", "지배기업의소유주에게귀속되는당기순이익(손실)"), name_re=r"지배기업.*(소유주|지분)")),
    ):
        r = pick(rows, IS, **kw)
        if key == "niP" and r and r.get("sj_div") in IS:
            # '지배기업 소유주지분'이 포괄손익 쪽에만 있으면 포괄이익일 수 있음 → 순이익 바로 아래 것 우선
            pass
        c, p = isv(r)
        out[key] = c
        out[key + "_p"] = p
    # 감가상각비(현금흐름표 조정항목 합계: 유형·사용권·무형 상각)
    da_c = da_p = 0.0
    seen = set()
    for r in rows:
        if r.get("sj_div") != "CF":
            continue
        nm = re.sub(r"\s", "", r.get("account_nm", ""))
        if re.search(r"(감가상각비|상각비)$", nm) and not re.search(r"환입|손상|처분|대손", nm) and nm not in seen:
            seen.add(nm)
            c = num(r.get("thstrm_amount"))
            p = num(r.get("frmtrm_amount"))
            da_c += c or 0
            da_p += p or 0
    out["da"] = da_c or None
    out["da_p"] = da_p or None
    BS = ("BS",)
    for key, kw in (
        ("equity", dict(ids=("ifrs-full_Equity", "ifrs_Equity"), names=("자본총계",))),
        ("equityP", dict(ids=("ifrs-full_EquityAttributableToOwnersOfParent", "ifrs_EquityAttributableToOwnersOfParent"), names=("지배기업소유주지분", "지배기업의소유주에게귀속되는자본"))),
        ("assets", dict(ids=("ifrs-full_Assets", "ifrs_Assets"), names=("자산총계",))),
        ("cash", dict(ids=("ifrs-full_CashAndCashEquivalents", "ifrs_CashAndCashEquivalents"), names=("현금및현금성자산",))),
    ):
        r = pick(rows, BS, **kw)
        out[key] = num(r.get("thstrm_amount")) if r else None
    # 단기금융상품 등(순차입금 계산 때 현금성으로 봄)
    stf = 0.0
    debt = 0.0
    lease = 0.0
    seen = set()
    for r in rows:
        if r.get("sj_div") != "BS":
            continue
        nm = re.sub(r"\s", "", r.get("account_nm", ""))
        v = num(r.get("thstrm_amount")) or 0
        if nm in seen:
            continue
        if re.fullmatch(r"(유동|비유동)?단기(금융상품|금융자산|투자자산)|단기금융상품", nm):
            stf += v; seen.add(nm)
        elif re.search(r"(차입금|사채)$", nm) and not re.search(r"합계|총계|부채$|상환|할인|할증|전환권|신주인수권|조정", nm):
            debt += v; seen.add(nm)
        elif re.search(r"리스부채$", nm) and not re.search(r"합계|총계", nm):
            lease += v; seen.add(nm)
    out["stFin"] = stf or None
    out["debt"] = debt or None
    out["lease"] = lease or None
    return out


def fetch_fin(dart, corp, year, rc):
    """한 보고서: 연결 우선, 없으면 별도"""
    for fs in ("CFS", "OFS"):
        j = dart.get("fnlttSinglAcntAll.json", corp_code=corp, bsns_year=str(year), reprt_code=rc, fs_div=fs)
        if j.get("status") == "000" and j.get("list"):
            rows = j["list"]
            d = parse_fs(rows, annual=(rc == "11011"))
            if not any(d.get(k) is not None for k in ("rev", "op", "ni")):
                continue
            d["fs"] = "연결" if fs == "CFS" else "별도"
            d["rcept"] = rows[0].get("rcept_no")
            return d
        if j.get("status") not in ("013", "000"):
            return {"err": j.get("status"), "msg": j.get("message")}
    return None


def update_fin(companies, cmap):
    if not DART_KEY:
        log("[재무] DART_API_KEY 가 없어 건너뜀")
        return
    FIN.mkdir(parents=True, exist_ok=True)
    dart = DartBudget(DART_BUDGET)
    want = wanted_reports()
    log(f"[재무] 받을 보고서: {[(y, REPORTS[r]) for y, r in want]}")
    codes = sorted(companies, key=lambda c: (companies[c]["market"] != "코스닥", c))   # 코스닥 먼저(IPO 비교회사 대부분)
    done_corp = 0
    try:
        for code in codes:
            if mins() > TIME_LIMIT_MIN:
                log("[재무] 시간 제한 → 나머지는 다음 실행에")
                break
            corp = cmap.get(code)
            if not corp:
                continue
            p = FIN / f"{code}.json"
            cur = json.loads(p.read_text("utf-8")) if p.exists() else {"code": code, "corp": corp, "r": {}}
            if cur.get("ver") != FIN_VER:
                cur["r"], cur["ver"] = {}, FIN_VER
            changed = False
            for y, rc in want:
                k = f"{y}{REPORTS[rc]}"
                old = cur["r"].get(k)
                # 이미 받았으면 건너뜀. '없음'은 30일 지나면 다시 확인(늦게 낸 보고서)
                if old and (not old.get("none") or (TODAY - date.fromisoformat(old["at"])).days < 30):
                    continue
                d = fetch_fin(dart, corp, y, rc)
                if d is None:
                    cur["r"][k] = {"none": True, "at": TODAY.isoformat()}
                elif d.get("err"):
                    continue
                else:
                    d["at"] = TODAY.isoformat()
                    cur["r"][k] = d
                changed = True
            if changed:
                p.write_text(json.dumps(cur, ensure_ascii=False, separators=(",", ":")), "utf-8")
                done_corp += 1
    except StopIteration as e:
        log(f"[재무] {e} → 나머지는 다음 실행에")
    log(f"[재무] 회사 {done_corp}개 갱신 · DART 호출 {dart.calls}회")


# ---------------------------------------------------------------- 저장
def main():
    DATA.mkdir(exist_ok=True)
    comp_p = DATA / "companies.json"
    old = {}
    if comp_p.exists():
        try:
            old = {c["code"]: c for c in json.loads(comp_p.read_text("utf-8"))["items"]}
        except Exception:
            old = {}
    try:
        companies = kind_corp_list()
    except Exception as e:
        log(f"[회사] KIND 실패: {e} → 지난 목록 사용")
        companies = old
    cmap = {}
    if DART_KEY:
        try:
            cmap = dart_corp_map()
        except Exception as e:
            log(f"[DART] 고유번호 실패: {e}")
    for code, c in companies.items():
        c["corp"] = cmap.get(code) or (old.get(code) or {}).get("corp")
    try:
        update_prices(companies)
    except Exception as e:
        log(f"[주가] 실패: {e}")
    try:
        update_fin(companies, {k: v["corp"] for k, v in companies.items() if v.get("corp")})
    except Exception as e:
        log(f"[재무] 실패: {e}")
    items = []
    for code, c in sorted(companies.items()):
        c["px"] = (PX / f"{code}.json").exists()
        c["fin"] = (FIN / f"{code}.json").exists()
        items.append(c)
    st = {}
    try:
        st = json.loads((DATA / "px_state.json").read_text("utf-8"))
    except Exception:
        pass
    comp_p.write_text(json.dumps({"updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "pxLast": st.get("last"),
                                  "items": items}, ensure_ascii=False, separators=(",", ":")), "utf-8")
    log(f"[저장] 회사 {len(items)}개 · 주가 {sum(c['px'] for c in items)}개 · 재무 {sum(c['fin'] for c in items)}개 · {mins():.0f}분")


if __name__ == "__main__":
    main()
