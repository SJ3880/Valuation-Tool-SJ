"""
사이트 잠금: 배포 직전에 index.html 을 비밀번호로 암호화해서 '잠금 화면'으로 바꿔 올림(저장소의 원본은 그대로)

  · 비밀번호는 GitHub Secrets 의 SITE_PASSWORDS 에 한 줄에 한 명씩:   이름:비밀번호
      예)  송준:apple123
           태영대리님:banana77
  · 사람마다 비밀번호를 따로 주면, 그 줄만 지우고 다시 배포했을 때 그 사람만 못 들어오게 됨
  · SITE_PASSWORDS 가 비어 있으면 잠그지 않음(경고만 출력)
  · 암호화: 페이지 본문은 무작위 키(AES-256-GCM)로, 그 키는 사람별 비밀번호(PBKDF2-SHA256 25만 번)로 감쌈
"""
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "index.html"
ITER = 250_000

b64 = lambda b: base64.b64encode(b).decode()


def entries(raw):
    out = []
    for i, line in enumerate(l.strip() for l in (raw or "").splitlines()):
        if not line or line.startswith("#"):
            continue
        name, _, pw = line.partition(":") if ":" in line else (f"사용자{i + 1}", "", line)
        if pw.strip():
            out.append((name.strip() or f"사용자{i + 1}", pw.strip()))
    return out


def main():
    users = entries(os.environ.get("SITE_PASSWORDS"))
    if not users:
        print("::warning::SITE_PASSWORDS 가 비어 있어 사이트를 잠그지 않았습니다(누구나 접속 가능).")
        return
    html = PAGE.read_bytes()
    if b'id="pv-lock"' in html:
        print("이미 잠긴 페이지라 건너뜀")
        return
    key, iv = AESGCM.generate_key(256), os.urandom(12)
    body = AESGCM(key).encrypt(iv, html, None)
    slots = []
    for name, pw in users:
        salt = hashlib.sha256(f"pv-lock:{name}".encode()).digest()[:16]   # 사람별로 고정 → '이 기기 기억'이 배포 뒤에도 유지
        wk = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(pw.encode())
        i2 = os.urandom(12)
        slots.append({"s": b64(salt), "i": b64(i2), "c": b64(AESGCM(wk).encrypt(i2, key, None))})
    slots.sort(key=lambda x: x["c"])                                      # 순서로 누가 누군지 알 수 없게
    payload = json.dumps({"n": ITER, "iv": b64(iv), "d": b64(body), "slots": slots}, separators=(",", ":"))
    tpl = (ROOT / "scripts" / "lock_template.html").read_text("utf-8")
    PAGE.write_text(tpl.replace("/*PAYLOAD*/null", payload), "utf-8")
    print(f"사이트 잠금 완료: 접속 가능한 사람 {len(users)}명")


if __name__ == "__main__":
    sys.exit(main())
