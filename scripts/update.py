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
        if mins() > TIME_LIMIT_MIN * (0.95 if os.environ.get("ONLY_PRICES") else 0.45):
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
            if code in companies and not companies[code].get("_sectSet"):
                companies[code]["isuName"] = r.get("ISU_NM")
                companies[code]["sect"] = (r.get("SECT_TP_NM") or "").strip() or None   # 코스닥 소속부(관리종목 등)
                companies[code]["sectAt"] = ds
                companies[code]["_sectSet"] = True
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
        import threading
        self.left = n
        self.calls = 0
        self.lock = threading.Lock()

    def get(self, path, **params):
        if self.left <= 0:
            raise StopIteration("DART 호출 한도")
        params["crtfc_key"] = DART_KEY
        for attempt in range(3):
            try:
                r = requests.get(f"{DART_API}/{path}", params=params, timeout=60)
                r.raise_for_status()
                with self.lock:
                    self.left -= 1
                    self.calls += 1
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
    """LTM 계산에 필요한 것만: 최근 사업보고서 + 그 뒤 가장 최근 분기·반기보고서(제출기한 지난 것)"""
    y = TODAY.year
    fy = y - 1 if TODAY > date(y, 3, 31) else y - 2
    out = [(fy, "11011")]
    for (yy, rc, end) in ((y, "11014", date(y, 11, 14)), (y, "11012", date(y, 8, 14)), (y, "11013", date(y, 5, 15)),
                          (y - 1, "11014", date(y - 1, 11, 14))):
        if TODAY > end and yy > fy:
            out.append((yy, rc))
            break
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


def update_fin(companies, cmap, dart):
    if not dart:
        log("[재무] DART_API_KEY 가 없어 건너뜀")
        return
    FIN.mkdir(parents=True, exist_ok=True)
    want = wanted_reports()
    log(f"[재무] 받을 보고서: {[(y, REPORTS[r]) for y, r in want]}")
    codes = sorted(companies, key=lambda c: (companies[c]["market"] != "코스닥", c))   # 코스닥 먼저(IPO 비교회사 대부분)
    from concurrent.futures import ThreadPoolExecutor
    stop = {"why": None}

    def one(code):
        if stop["why"]:
            return 0
        if mins() > TIME_LIMIT_MIN * 0.75:
            stop["why"] = "시간 제한"
            return 0
        if dart.left < 2500:                # 메타(감사의견 등)·매출비중 몫 남겨둠
            stop["why"] = "이번 실행 몫 다 씀"
            return 0
        corp = cmap.get(code)
        if not corp:
            return 0
        p = FIN / f"{code}.json"
        cur = json.loads(p.read_text("utf-8")) if p.exists() else {"code": code, "corp": corp, "r": {}}
        if cur.get("ver") != FIN_VER:
            cur["r"], cur["ver"] = {}, FIN_VER
        changed = False
        try:
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
        except StopIteration as e:
            stop["why"] = str(e)
        except Exception as e:
            log(f"  - {code} 재무 실패: {e}")
        if changed:
            p.write_text(json.dumps(cur, ensure_ascii=False, separators=(",", ":")), "utf-8")
            return 1
        return 0

    with ThreadPoolExecutor(max_workers=6) as ex:
        done_corp = sum(ex.map(one, codes))
    if stop["why"]:
        log(f"[재무] {stop['why']} → 나머지는 다음 실행에")
    log(f"[재무] 회사 {done_corp}개 갱신 · DART 호출 {dart.calls}회")



# ---------------------------------------------------------------- 4) 회사 메타(표준산업분류·감사의견)
META_P = DATA / "meta.json"


def load_meta():
    try:
        return json.loads(META_P.read_text("utf-8"))
    except Exception:
        return {}


def update_meta(companies, cmap, dart, meta):
    """DART 기업개황(표준산업분류 코드)·최근 사업연도 감사의견. 한 번 받으면 1년에 한 번만 다시"""
    fy = TODAY.year - 1 if TODAY.month >= 4 else TODAY.year - 2
    from concurrent.futures import ThreadPoolExecutor
    stop = {"why": None}

    def one(code):
        if stop["why"] or mins() > TIME_LIMIT_MIN or dart.left < 300:
            return 0
        corp = cmap.get(code)
        if not corp:
            return 0
        m = dict(meta.get(code) or {})
        k = 0
        try:
            if not m.get("ksic"):
                j = dart.get("company.json", corp_code=corp)
                if j.get("status") == "000":
                    m["ksic"] = j.get("induty_code") or None
                    m["est"] = j.get("est_dt") or None
                    k += 1
            if (m.get("audit") or {}).get("year") != fy:
                j = dart.get("accnutAdtorNmNdAdtOpinion.json", corp_code=corp, bsns_year=str(fy), reprt_code="11011")
                if j.get("status") == "000" and j.get("list"):
                    row = next((r for r in j["list"] if "당기" in (r.get("bsns_year") or "")), j["list"][0])
                    m["audit"] = {"year": fy, "opinion": (row.get("adt_opinion") or "").strip() or None,
                                  "auditor": (row.get("adtor") or "").strip() or None}
                    k += 1
                elif j.get("status") == "013":
                    m["audit"] = {"year": fy, "opinion": None}
        except StopIteration as e:
            stop["why"] = str(e)
        except Exception as e:
            log(f"  - {code} 메타 실패: {e}")
        meta[code] = m
        return k

    codes = sorted(companies, key=lambda c: (companies[c]["market"] != "코스닥", c))
    with ThreadPoolExecutor(max_workers=6) as ex:
        n = sum(ex.map(one, codes))
    if stop["why"]:
        log(f"[메타] {stop['why']}")
    log(f"[메타] 표준산업분류·감사의견 {n}건 갱신")


# ---------------------------------------------------------------- 5) 주요 제품 매출 비중(사업보고서 본문)
def strip_tags(x):
    import html as _h
    return re.sub(r"\s+", " ", _h.unescape(re.sub(r"<[^>]+>", " ", x))).strip()


def parse_mix(xml):
    """사업보고서 '주요 제품(및 서비스)' 표에서 품목·매출비중 뽑기"""
    i = xml.find("주요 제품")
    if i < 0:
        i = xml.find("주요제품")
    if i < 0:
        return None
    seg = xml[i: i + 200000]
    j = seg.find("원재료")
    if j > 0:
        seg = seg[:j]
    for tbl in re.findall(r"<TABLE.*?</TABLE>", seg, flags=re.S | re.I)[:6]:
        rows = [[strip_tags(c) for c in re.findall(r"<T[DHE][^>]*>(.*?)</T[DHE]>", tr, flags=re.S | re.I)]
                for tr in re.findall(r"<TR.*?</TR>", tbl, flags=re.S | re.I)]
        rows = [r for r in rows if any(r)]
        if len(rows) < 2:
            continue
        hdr_i = next((k for k, r in enumerate(rows[:3]) if any(re.search(r"비율|비중|%", c) for c in r)), None)
        if hdr_i is None:
            continue
        hdr = [c.replace(" ", "") for c in rows[hdr_i]]
        pc = next((k for k, c in enumerate(hdr) if re.search(r"비율|비중", c)), None)
        if pc is None:
            pc = next((k for k, c in enumerate(hdr) if "%" in c), None)
        nc = next((k for k, c in enumerate(hdr) if re.search(r"품목|제품|품명|상품", c)), None)
        if nc is None:
            nc = next((k for k, c in enumerate(hdr) if re.search(r"부문|구분", c)), 0)
        out = []
        isnum = lambda t: bool(re.fullmatch(r"[\s\d,.\-()%△▲]*", t or ""))
        total = lambda t: bool(re.search(r"합계|총계|소계|^계$|^합$|전체", (t or "").replace(" ", "")))
        for r in rows[hdr_i + 1:]:
            off = len(hdr) - len(r)                 # 병합된 행은 앞쪽 칸이 빠져 있으므로 끝에서부터 맞춤
            pi = pc - off
            if not (0 <= pi < len(r)):
                continue
            # 이름: 비율 칸 왼쪽에서 숫자가 아닌 가장 오른쪽 글자 칸(금액 칸은 건너뜀)
            cand = [t for t in r[:pi] if t and not isnum(t)]
            if not cand:
                continue
            name = cand[-1]
            if any(total(t) for t in r[:pi]) or total(name):
                continue
            v = re.sub(r"[^0-9.\-]", "", r[pi])
            try:
                pct = float(v)
            except ValueError:
                continue
            if 0 < pct < 100:
                out.append({"n": re.sub(r"\s+", " ", name)[:40], "p": round(pct, 2)})
        if out:
            out.sort(key=lambda x: -x["p"])
            return out[:6]
    return None


MIX_VER = 2
BIZ_VER = 1
BIZ_P = DATA / "biztxt.json"          # 사업보고서 '사업의 개요' 본문(모집단 자동 추천용, 화면에는 안 씀)


def parse_biz(xml):
    """사업보고서 'II. 사업의 내용'에서 사업의 개요 ~ 주요 제품 부분 글자만(숫자 빼고) 앞 5천자"""
    m = re.search(r"<TITLE[^>]*>\s*(?:II|Ⅱ|2)\s*\.\s*사업의\s*내용", xml)
    s = xml[m.start():] if m else xml
    i = s.find("사업의 개요")
    if i < 0:
        i = s.find("사업의 내용")
    if i < 0:
        return None
    seg = s[i: i + 150000]
    ends = [e.start() for e in (re.search(r"<TITLE[^>]*>\s*(?:3|4|5)\s*\.\s*(?:원재료|생산|매출)", seg),
                                re.search(r"<TITLE[^>]*>\s*(?:III|Ⅲ)\s*\.", seg)) if e]
    if ends:
        seg = seg[:min(ends)]
    t = strip_tags(seg)
    t = re.sub(r"[0-9][0-9,.\-%]*", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:5000] or None


def load_biz():
    try:
        return json.loads(BIZ_P.read_text("utf-8"))
    except Exception:
        return {}


def update_mix(companies, dart, meta, biz):
    """최근 사업보고서 본문을 한 번만 읽어 주요 제품 매출 비중 + 사업의 개요 저장(1년에 한 번)"""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    lock = threading.Lock()
    cnt = {"tried": 0, "mix": 0, "biz": 0}
    jobs = []
    for code in sorted(companies, key=lambda c: (c in biz, companies[c]["market"] != "코스닥", c)):   # 개요 없는 회사 먼저
        fp = FIN / f"{code}.json"
        if not fp.exists():
            continue
        try:
            fin = json.loads(fp.read_text("utf-8"))
        except Exception:
            continue
        fys = sorted((k for k, v in fin.get("r", {}).items() if k.endswith("FY") and v and v.get("rcept")), reverse=True)
        if not fys:
            continue
        rc = fin["r"][fys[0]]["rcept"]
        mm = meta.get(code) or {}
        mix_ok = (mm.get("mix") or {}).get("rcept") == rc and (mm.get("mix") or {}).get("v") == MIX_VER
        biz_ok = (biz.get(code) or {}).get("rc") == rc and (biz.get(code) or {}).get("v") == BIZ_VER
        if not (mix_ok and biz_ok):
            jobs.append((code, rc, fys[0][:4]))

    def one(job):
        code, rc, yr = job
        if mins() > TIME_LIMIT_MIN or dart.left < 50:
            return
        try:
            r = requests.get(f"{DART_API}/document.xml", params={"crtfc_key": DART_KEY, "rcept_no": rc}, timeout=120)
            with dart.lock:
                dart.left -= 1
                dart.calls += 1
            try:
                z = zipfile.ZipFile(io.BytesIO(r.content))
            except zipfile.BadZipFile:
                if b'"020"' in r.content[:300]:
                    dart.left = 0
                    return
                with lock:
                    meta.setdefault(code, {})["mix"] = {"rcept": rc, "year": yr, "items": None, "v": MIX_VER}
                    biz[code] = {"rc": rc, "v": BIZ_VER, "t": None}
                return
            main = max(z.namelist(), key=lambda nm: z.getinfo(nm).file_size)
            raw = z.read(main)
            xml = raw.decode("utf-8", errors="ignore")
            if "사업" not in xml:
                xml = raw.decode("euc-kr", errors="ignore")
            mix, txt = parse_mix(xml), parse_biz(xml)
        except Exception as e:
            log(f"  - {code} 사업보고서 실패: {e}")
            return
        with lock:
            meta.setdefault(code, {})["mix"] = {"rcept": rc, "year": yr, "items": mix, "v": MIX_VER}
            biz[code] = {"rc": rc, "v": BIZ_VER, "t": txt}
            cnt["tried"] += 1
            cnt["mix"] += bool(mix)
            cnt["biz"] += bool(txt)

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(one, jobs))
    log(f"[사업보고서] 대상 {len(jobs)}건 중 {cnt['tried']}건 읽음 · 매출비중 표 {cnt['mix']}건 · 사업의 개요 {cnt['biz']}건")


# ---------------------------------------------------------------- 5-1) 모집단 자동 추천용 색인
JOSA = sorted("하였습니다 했습니다 합니다 됩니다 입니다 하였으며 하였고 하며 되며 되고 이다 하였다 했다 으로부터 로부터 이라는 라는 에는 와의 과의 에서는 으로는 에서도 으로서 으로써 에서 으로 에게 까지 부터 이며 이고 하는 하여 하고 한다 된다 되는 되어 적인 적으로 들의 들은 들이 들을 은 는 을 를 의 에 와 과 용 및 등".split(), key=len, reverse=True)
STOP = set("당사 회사 동사 사업 제품 서비스 기타 주요 관련 국내 해외 시장 매출 분야 부문 업체 기업 통해 위해 대한 있는 있습니다 합니다 하였습니다 있으며 경우 이상 이하 현재 당기 전기 기준 사용 제공 생산 판매 개발 제조".split())

STOP_EN = set("a an the and or of to in on for with by from at as is are was were be been being it its this that these those any all some other such than then there their they we our you your he she his her not no yes can may will would should could do does did done has have had also into over under about after before between more most less new novel based using use used via per each early late pre post up out etc inc co ltd corp llc company year years deal date ipo vs ex eg ie key core global world market total part plan".split())


def tokens(text):
    """한글은 2글자씩 끊은 조각 + 3~6글자 낱말, 영문·숫자 섞인 말은 낱말 그대로 (화면 쪽 tokens()와 똑같이)"""
    out = []
    for w in re.findall(r"[가-힣]+|[a-z0-9]+", (text or "").lower()):
        if not re.search(r"[가-힣]", w):
            if len(w) >= 2 and not w.isdigit() and w not in STOP_EN:
                out.append(w)
            continue
        h = w
        for j in JOSA:
            if h.endswith(j) and len(h) - len(j) >= 2:
                h = h[: -len(j)]
                break
        if len(h) < 2 or h in STOP:
            continue
        if 3 <= len(h) <= 6:
            out += [h, h]                      # 낱말 전체가 맞으면 조각보다 2배
        for k in range(len(h) - 1):
            out.append(h[k: k + 2])
    return out


def ksic_names():
    try:
        return json.loads((DATA / "ksic.json").read_text("utf-8"))
    except Exception:
        return {}


def ksic_chain(code, names):
    """C26299 → 대분류 밑 중·소·세·세세분류 이름들"""
    code = (code or "").lstrip("C")
    return [names[code[:k]] for k in range(2, len(code) + 1) if code[:k] in names]


def build_biz_index(items, biz):
    """회사마다 업종·주요제품·매출비중 품목·사업의 개요로 낱말 점수표(TF-IDF) → data/biz.json"""
    import math
    from collections import Counter
    names = ksic_names()
    docs = {}
    for c in items:
        parts = [(c.get("product") or "") + " ", (c.get("sector") or "")] * 3          # 짧지만 정확한 말은 무게를 더
        parts += [" ".join(ksic_chain(c.get("ksic"), names))] * 2
        parts += [" ".join(m["n"] for m in (c.get("mix") or []))] * 2
        parts.append((biz.get(c["code"]) or {}).get("t") or "")
        tf = Counter(tokens(" ".join(parts)))
        if tf:
            docs[c["code"]] = tf
    N = len(docs)
    df = Counter()
    for tf in docs.values():
        df.update(tf.keys())
    idf = {t: math.log((N + 1) / (n + 0.5)) for t, n in df.items() if 1 < n <= N * 0.3}   # 한 회사만 쓰는 말·너무 흔한 말은 뺌
    out, used = {}, set()
    for code, tf in docs.items():
        w = {t: (1 + math.log(n)) * idf[t] for t, n in tf.items() if t in idf}
        top = sorted(w.items(), key=lambda x: -x[1])[:80]
        nrm = math.sqrt(sum(v * v for _, v in top)) or 1
        out[code] = [[t, round(v / nrm, 3)] for t, v in top if v / nrm >= 0.01]
        used.update(t for t, _ in out[code])
    (DATA / "biz.json").write_text(json.dumps({"v": 1, "updated": TODAY.isoformat(), "n": N,
                                               "withText": sum(1 for x in biz.values() if x.get("t")),
                                               "idf": {t: round(idf[t], 2) for t in sorted(used)}, "docs": out},
                                              ensure_ascii=False, separators=(",", ":")), "utf-8")
    log(f"[추천 색인] 회사 {len(out)}개 · 낱말 {len(used)}개 · 사업의 개요 반영 {sum(1 for x in biz.values() if x.get('t'))}개")


# ---------------------------------------------------------------- 6) 공모 할인율(IPO 동향 사이트 데이터)
def ipo_discounts():
    """IPO 동향 분석 사이트(SJ3880/IPO-Investing)의 상장완료 데이터에서 신고서 할인율·트랙"""
    url = "https://raw.githubusercontent.com/SJ3880/IPO-Investing/main/data/ipos.json"
    try:
        items = requests.get(url, timeout=60).json()["items"]
    except Exception as e:
        log(f"[할인율] IPO 사이트 데이터 실패: {e}")
        return
    out = []
    for x in items:
        b = x.get("discBand")
        if not b or x.get("spac"):
            continue
        out.append({"name": x.get("name"), "code": x.get("code"), "listed": x.get("listed"), "market": x.get("market"),
                    "track": x.get("track"), "listType": x.get("listType"), "lo": b[0], "hi": b[1],
                    "method": x.get("valMethod"), "mult": x.get("valMult"), "sector": x.get("sector"),
                    "peers": [p.get("n") for p in (x.get("peers") or [])][:12]})
    out.sort(key=lambda r: r["listed"] or "", reverse=True)
    (DATA / "ipo_disc.json").write_text(json.dumps({"updated": TODAY.isoformat(), "items": out}, ensure_ascii=False,
                                                    separators=(",", ":")), "utf-8")
    log(f"[할인율] 공모 사례 {len(out)}건")


# ---------------------------------------------------------------- 저장
def prices_only():
    """주가만 받기(다른 수집과 따로, 바로 돌릴 때)"""
    comp_p = DATA / "companies.json"
    companies = {c["code"]: c for c in json.loads(comp_p.read_text("utf-8"))["items"]}
    update_prices(companies)
    n = len(list(PX.glob("*.json"))) if PX.exists() else 0
    log(f"[주가만] 종목 파일 {n}개 · {mins():.0f}분")


def main():
    if os.environ.get("ONLY_PRICES"):
        return prices_only()
    DATA.mkdir(exist_ok=True)
    comp_p = DATA / "companies.json"
    old = {}
    meta = load_meta()
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
    ccorp = {k: v["corp"] for k, v in companies.items() if v.get("corp")}
    dart = DartBudget(DART_BUDGET) if DART_KEY else None
    try:
        update_fin(companies, ccorp, dart)
    except Exception as e:
        log(f"[재무] 실패: {e}")
    meta = load_meta()
    if dart:
        for fn in (update_meta, ):
            try:
                fn(companies, ccorp, dart, meta)
            except Exception as e:
                log(f"[메타] 실패: {e}")
        biz = load_biz()
        try:
            update_mix(companies, dart, meta, biz)
        except Exception as e:
            log(f"[사업보고서] 실패: {e}")
        META_P.write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), "utf-8")
        BIZ_P.write_text(json.dumps(biz, ensure_ascii=False, separators=(",", ":")), "utf-8")
    try:
        ipo_discounts()
    except Exception as e:
        log(f"[할인율] 실패: {e}")
    items = []
    for code, c in sorted(companies.items()):
        c.pop("_sectSet", None)
        if not c.get("sect") and (old.get(code) or {}).get("sect"):
            c["sect"], c["sectAt"] = old[code]["sect"], old[code].get("sectAt")
        c["px"] = (PX / f"{code}.json").exists()
        c["fin"] = (FIN / f"{code}.json").exists()
        m = meta.get(code) or {}
        c["ksic"] = m.get("ksic")
        c["audit"] = (m.get("audit") or {}).get("opinion")
        c["auditYear"] = (m.get("audit") or {}).get("year")
        mix = (m.get("mix") or {}).get("items") if (m.get("mix") or {}).get("v") == MIX_VER else None
        c["mix"] = mix or None
        c["mixYear"] = (m.get("mix") or {}).get("year") if mix else None
        items.append(c)
    st = {}
    try:
        st = json.loads((DATA / "px_state.json").read_text("utf-8"))
    except Exception:
        pass
    comp_p.write_text(json.dumps({"updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "pxLast": st.get("last"),
                                  "items": items}, ensure_ascii=False, separators=(",", ":")), "utf-8")
    try:
        build_biz_index(items, load_biz())
    except Exception as e:
        log(f"[추천 색인] 실패: {e}")
    log(f"[저장] 회사 {len(items)}개 · 주가 {sum(c['px'] for c in items)}개 · 재무 {sum(c['fin'] for c in items)}개 · {mins():.0f}분")


if __name__ == "__main__":
    main()
