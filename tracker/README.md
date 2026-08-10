# 장어 가격 추적기

거래처 몰의 특정 상품 가격과 시장 시세(양념민물장어, 냉동민물장어필렛)를
매일 자동으로 수집해 이력을 쌓는 도구입니다. GitHub Actions가 **매일 07:30 KST**에
실행하며, 결과는 이 폴더의 CSV 파일에 커밋됩니다. 가격 변동이 감지되면
저장소에 이슈가 자동 생성됩니다(저장소 Watch 시 메일 알림).

## 구성

| 파일 | 역할 |
|---|---|
| `track_price.py` | 지정 상품 정밀 추적 (푸드엔 등 개별 상품 페이지) |
| `products.csv` | 정밀 추적할 상품 목록 (URL 추가/삭제) |
| `price_history.csv` | 지정 상품 일별 가격 이력 |
| `market_price.py` | 네이버쇼핑 시장시세 수집 (키워드 검색 → kg당 가격 통계) |
| `market_keywords.csv` | 시세를 볼 품목 키워드 목록 |
| `market_history.csv` | 품목별 일별 시세 통계 (최저/중앙값/평균, kg당) |
| `market_listings.csv` | 당일 상위 판매글 스냅샷 (판매처·가격·중량) |

## 네이버 API 키 등록 (시장시세 수집에 필요, 5분)

시장시세 모듈은 네이버 쇼핑 검색 API를 사용합니다. 키를 등록하기 전에는
해당 단계만 건너뛰고 나머지는 정상 동작합니다.

1. https://developers.naver.com 로그인 → 우측 상단 **Application → 애플리케이션 등록**
2. 애플리케이션 이름: 아무거나 (예: `price-tracker`)
   사용 API: **검색** 선택
   환경: **WEB 설정** 선택, 웹 서비스 URL에 `https://github.com` 입력 → 등록
3. 발급된 **Client ID**와 **Client Secret** 두 값을 복사
4. GitHub 저장소 → **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `NAVER_CLIENT_ID`, Secret: (Client ID 값)
   - Name: `NAVER_CLIENT_SECRET`, Secret: (Client Secret 값)

등록 후 다음 실행부터 시장시세가 수집됩니다. 무료이며 일 25,000회 한도 중
하루 2회만 사용합니다.

## 시장시세 산출 방식

- 키워드별로 상위 100개 판매글을 받아 다른 어종(붕장어·바다장어 등)과
  소스/진액류를 제목 기준으로 걸러냅니다.
- 상품명에서 중량(`1kg`, `500g x 2` 등)을 인식해 **kg당 가격으로 환산**하고,
  일별 최저/중앙값/평균을 기록합니다. 중량을 인식하지 못한 판매글은
  통계에서 제외하되 스냅샷에는 남깁니다.
- 전일 대비 **중앙값이 5% 이상** 움직이면 이슈 알림이 생성됩니다.
- 판매글 단위 데이터가 `market_listings.csv`에 남으므로 이상치는 언제든
  역추적할 수 있습니다.

## 상품/키워드 추가

- 특정 상품 추적: `products.csv`에 `라벨,URL` 한 줄 추가
  (라벨은 이력 파일의 키이므로 한 번 정하면 유지)
- 시세 품목 추가: `market_keywords.csv`에 `라벨,검색어,필수어(정규식),추가제외어` 추가

## 주의사항

- **스케줄 실행은 저장소 기본 브랜치의 워크플로우만 작동합니다.**
- 가격이 로그인 후에만 보이는 몰은 시크릿 `FOODEN_COOKIE`에 세션 쿠키를
  등록하면 수집됩니다 (만료 시 갱신 필요).
- 수집은 하루 1회, 등록된 페이지/공식 API만 사용합니다. robots.txt가
  금지하는 경로는 건너뜁니다.
- 과거(추적 시작 이전) 가격은 소급할 수 없습니다. 이력은 시작일부터 쌓입니다.
- 산지(원물) 시세는 KMI 수산물 수급정보(fishdata.kmi.re.kr) 연동을 검토 중입니다.

## 로컬 실행

```bash
python3 tracker/track_price.py
NAVER_CLIENT_ID=... NAVER_CLIENT_SECRET=... python3 tracker/market_price.py
```
