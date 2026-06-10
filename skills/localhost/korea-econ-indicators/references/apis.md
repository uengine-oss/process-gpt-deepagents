# API 참고(엔드포인트)

## 1) 네이버 주식(종목 통합 정보)

- **엔드포인트**: `https://m.stock.naver.com/api/stock/{itemcode}/integration`
- **형식**: JSON
- **사용**: 종목코드(6자리)를 아는 경우 가장 안정적으로 현재가/요약 정보를 얻을 수 있다.
- **주의**: 코스피·코스닥 **종합지수**는 이 경로에 종목코드 형태로 넣어도 응답이 안 되거나 HTTP **409** (`StockConflict`)가 날 수 있다. 지수는 아래 (2)를 사용한다.

## 2) 네이버 지수(코스피·코스닥 종합)

- **엔드포인트**: `https://m.stock.naver.com/api/index/{KOSPI|KOSDAQ}/integration`
- **형식**: JSON
- **사용**: 지수명 그대로 `KOSPI` / `KOSDAQ` path 세그먼트. `totalInfos`의 `lastClosePrice`(표시 키 `전일`)는 직전 영업일 종가, 장중에는 당일 시세가 다른 필드에 채워진다.

## 3) 네이버 주식(종목명 → 종목코드 검색, API 기반)

정식 “검색” API가 공개되어 있지 않아, 스킬은 **거래소별 종목 리스트(JSON)** 를 가져와 종목명을 필터링하는 방식을 기본으로 한다.

- **엔드포인트(거래소별 시가총액 목록)**:
  - `https://api.stock.naver.com/stock/exchange/{KOSPI|KOSDAQ}/marketValue?page=1&pageSize=100`
- **형식**: JSON
- **주의**:
  - 응답 스키마(필드명)가 변경될 수 있으므로, 스크립트는 `stockName`/`itemCode` 등 여러 키 후보를 보수적으로 처리한다.
  - 기본 설정은 각 거래소 상위 `max_pages * page_size` 종목만 스캔한다(빠르게 찾기 위함). 못 찾으면 `--max-pages` 를 올린다.

## 4) 한국은행 ECOS OpenAPI(주요 경제지표 스냅샷)

- **엔드포인트(주요지표 목록)**: `https://ecos.bok.or.kr/api/KeyStatisticList/{API_KEY}/json/kr/{start}/{end}`
- **형식**: JSON
- **권장 사용**:
  - 코스피/코스닥, GDP, CPI 같은 “대표 지표”는 `KeyStatisticList` 조회 후 `KEYSTAT_NAME` 필터로 찾는 방식이 가장 견고하다(코드 고정 의존을 줄임).

## 5) 한국은행 ECOS OpenAPI(시계열)

- **엔드포인트(시계열)**:
  - `https://ecos.bok.or.kr/api/StatisticSearch/{API_KEY}/json/kr/{startRow}/{endRow}/{STAT_CODE}/{CYCLE}/{START_DATE}/{END_DATE}/{ITEM_CODE?}`
- **형식**: JSON
- **주의**:
  - `STAT_CODE`/`ITEM_CODE`는 ECOS 웹에서 조회해 알아내야 한다(지표마다 다름).
  - 날짜 포맷은 주기(`CYCLE`)에 따라 달라질 수 있다.

