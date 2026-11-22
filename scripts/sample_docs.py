"""임시: 파서 개선용 사업보고서 원문 샘플 내려받기(매출비중 표를 못 읽은 회사 위주)"""
import io, json, os, random, zipfile
from pathlib import Path
import requests
D = Path("data"); out = Path("sample"); out.mkdir(exist_ok=True)
key = os.environ["DART_API_KEY"]
meta = json.loads((D / "meta.json").read_text("utf-8"))
items = json.loads((D / "companies.json").read_text("utf-8"))["items"]
random.seed(7)
miss = [c["code"] for c in items if not c.get("mix")]
have = [c["code"] for c in items if c.get("mix")]
pick = random.sample(miss, 34) + random.sample(have, 8) + [c["code"] for c in items if c["name"] in ("NICE", "CJ CGV", "삼성전자", "RFHIC")]
for code in pick:
    fp = D / "fin" / f"{code}.json"
    if not fp.exists(): continue
    fin = json.loads(fp.read_text("utf-8"))
    fys = sorted((k for k, v in fin.get("r", {}).items() if k.endswith("FY") and v and v.get("rcept")), reverse=True)
    if not fys: continue
    rc = fin["r"][fys[0]]["rcept"]
    r = requests.get("https://opendart.fss.or.kr/api/document.xml", params={"crtfc_key": key, "rcept_no": rc}, timeout=120)
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile:
        continue
    main = max(z.namelist(), key=lambda nm: z.getinfo(nm).file_size)
    (out / f"{code}.xml").write_bytes(z.read(main))
print(len(list(out.glob("*.xml"))))
