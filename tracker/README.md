# 가격 추적기 (푸드엔)

거래처 온라인몰([푸드엔](https://www.fooden.com), 부산 식자재 유통몰)의 특정 상품 가격을
매일 자동으로 수집해 이력을 쌓는 도구입니다.

## 동작 방식

1. GitHub Actions가 **매일 07:30 KST**(`30 22 * * *` UTC)에 `tracker/track_price.py`를 실행합니다.
2. 스크립트가 `tracker/products.csv`에 등록된 상품 페이지를 내려받아 가격을 추출합니다.
   (메타태그 → JSON-LD → 히든 인풋 → "판매가" 라벨 → "N원" 패턴 순으로 시도)
3. 결과가 `tracker/price_history.csv`에 날짜별로 커밋됩니다 — 이 파일이 곧 가격 이력입니다.
4. **전일 대비 가격이 바뀌면 저장소에 이슈가 자동 생성**됩니다. (저장소를 Watch하면 메일 알림)

## 상품 추가/삭제

`tracker/products.csv`에 행을 추가하거나 삭제하면 됩니다.

```csv
label,url
푸드엔-장어,https://www.fooden.com/shop/detail.php?pno=...&ctype=1
```

`label`은 이력 파일에서 상품을 구분하는 키이므로 한 번 정하면 바꾸지 않는 게 좋습니다.

## 주의사항

- **스케줄 실행은 저장소 기본 브랜치에서만 동작합니다.** 이 워크플로우가 기본 브랜치에
  병합되기 전에는 수동 실행(workflow_dispatch)과 브랜치 push 시에만 돕니다.
- 가격이 로그인 후에만 보이는 상품은 저장소 시크릿 `FOODEN_COOKIE`에 로그인 세션 쿠키를
  등록하면 수집할 수 있습니다. (세션이 만료되면 갱신 필요)
- 수집은 하루 1회, 등록된 상품 페이지만 접근합니다. robots.txt가 금지하는 경로는 건너뜁니다.
- 과거(추적 시작 이전) 가격은 소급해서 구할 수 없습니다. 이력은 추적 시작일부터 쌓입니다.

## 로컬 실행

```bash
python3 tracker/track_price.py
cat tracker/price_history.csv
```
