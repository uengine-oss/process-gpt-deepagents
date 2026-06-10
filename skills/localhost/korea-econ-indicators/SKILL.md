---
name: korea-econ-indicators
description: 한국 경제 지표(코스피/코스닥, GDP, CPI 등)와 한국 주식(종목코드 또는 종목명) 시세를 HTTP API 호출로 조회한다. 사용자가 코스피/코스닥, GDP/CPI, 물가/성장률, 또는 “삼성전자 주가”처럼 종목명/종목코드로 조회를 요청하면 반드시 이 스킬을 사용해 재현 가능한 API 호출(ECOS/네이버 주식 API)을 구성하고 실행 결과를 표로 요약한다.
---

# Korea Econ Indicators (KR)

## 지원 기능

- **종목코드로 주가 조회**: 네이버 모바일 주식 JSON API로 종목 통합 정보 조회
- **종목명으로 주가 조회**: 네이버 주식 종목 목록(JSON)을 가져와 종목명을 검색 → 종목코드로 시세 조회
- **코스피·코스닥 종합지수(장중 스냅샷)**: 네이버 `index/{KOSPI|KOSDAQ}/integration` (종목용 `stock/{6자리}` 경로는 지수에서 HTTP 409가 나므로 사용하지 않음)
- **코스피/코스닥/GDP/CPI 등 주요 경제지표 스냅샷**: 한국은행 ECOS `KeyStatisticList`(지수 “공식 통계” 성격이 필요하면 ECOS·시계열을 병행)
- **(선택) 시계열 조회**: ECOS `StatisticSearch`로 특정 통계표/항목 코드를 아는 경우 시계열 조회

> 원칙: “조회는 API 호출로 구현”을 지키기 위해, HTML 파싱/크롤링이 아니라 **HTTP API(JSON)** 응답만 사용한다.

## 준비물(권장)

- **ECOS API 키**: 한국은행 ECOS OpenAPI 키 (없으면 `KeyStatisticList` 기반 지표 조회는 제한됨)
  - 환경변수 `ECOS_API_KEY` 로 설정 권장

## 빠른 사용법

### 1) 종목코드로 현재가(요약) 조회

```bash
python3 scripts/korea_econ_api.py stock --code 005930
```

### 2) 종목명으로 현재가(요약) 조회

```bash
python3 scripts/korea_econ_api.py stock --name "삼성전자"
```

### 3) 코스피·코스닥 종합지수 조회(네이버)

```bash
python3 scripts/korea_econ_api.py index --market KOSPI
python3 scripts/korea_econ_api.py index --market KOSDAQ
python3 scripts/korea_econ_api.py index --all
```

### 4) 코스피/코스닥/GDP/CPI 등 주요지표 스냅샷 조회(ECOS)

```bash
export ECOS_API_KEY="YOUR_KEY"
python3 scripts/korea_econ_api.py key-stats --filter "코스피|코스닥|국내총생산|소비자물가"
```

### 5) ECOS 시계열 조회(통계표코드/주기/기간을 아는 경우)

```bash
export ECOS_API_KEY="YOUR_KEY"
python3 scripts/korea_econ_api.py ecos-series \
  --stat-code 200Y101 --cycle A --start 2015 --end 2024
```

## 에이전트 실행 지침(중요)

사용자가 “주가/지수/경제지표를 조회해줘”라고 하면 다음 순서로 진행한다.

1. **요구사항 정규화**
   - 종목코드인지 / 종목명인지 / 지수인지(코스피·코스닥) / 거시지표인지(GDP·CPI)
   - 기간 필요 여부(현재 스냅샷 vs 시계열)

2. **API 선택**
   - **개별 종목(종목코드)**: 네이버 `m.stock.naver.com/api/stock/{code}/integration`
   - **개별 종목(종목명)**: 네이버 `api.stock.naver.com/stock/exchange/{exchange}/marketValue`로 종목 리스트 조회 → 이름 매칭 → 위 integration 호출
   - **코스피·코스닥 “지수” 시세·전일 종가·장중 OHLC 요약**: 네이버 `m.stock.naver.com/api/index/{KOSPI|KOSDAQ}/integration` (`index` 서브커맨드)
   - **거시·공식 주요지표 스냅샷(코스피/코스닥/GDP/CPI 등)**: ECOS `KeyStatisticList`
   - **정확한 시계열이 필요하고 stat/item 코드를 아는 경우**: ECOS `StatisticSearch`

3. **실행**
   - 가능한 경우 `scripts/korea_econ_api.py`를 실행해 결과 JSON을 얻고, 사용자에게 표로 요약한다.

4. **출력 형식**
   - 최소한 아래 항목을 포함해 요약한다.
     - 데이터 출처(ECOS/네이버, 지수면 `index_integration` 여부 명시)
     - 조회 시각(로컬 기준)
     - 핵심 숫자(현재값·전일(`lastClosePrice`)/등락·단위)
   - 지수 JSON에서 `totalInfos`의 `lastClosePrice`(전일 종가), 당일 시가·고가·저가 등은 장 운영 여부에 따라 의미가 달라질 수 있으므로 `dealTrendInfo.bizdate` 등과 함께 해석한다.

## 추가 자료

- API 상세 및 엔드포인트: `references/apis.md`
- 예시 요청/응답 흐름: `examples.md`

