# Peer 밸류에이션 계산기

비교회사(Peer) 기준주가·재무로 PER·EV/EBITDA·PBR·PSR을 계산하고, 적용배수·할인율로 희망공모가 밴드를 구하는 도구.

- 주가: KRX Open API 일별매매정보(코스피·코스닥) — GitHub Secrets `KRX_API_KEY`
- 재무: DART 정기보고서 전체 재무제표 — GitHub Secrets `DART_API_KEY`
- 업종·주요제품: KIND 상장법인목록
- 매일 평일 08:30(KST) 자동 업데이트 (`.github/workflows/update.yml`)
