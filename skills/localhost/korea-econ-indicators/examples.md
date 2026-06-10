# 예시

## 1) “005930 지금 주가 알려줘”

```bash
python3 scripts/korea_econ_api.py stock --code 005930
```

## 2) “삼성전자 지금 주가 알려줘”(종목명만 아는 경우)

```bash
python3 scripts/korea_econ_api.py stock --name "삼성전자"
```

종목명이 애매해서 여러 개가 뜨면, 먼저 검색만 수행한다.

```bash
python3 scripts/korea_econ_api.py stock-search --name "삼성"
```

## 3) “지금 코스피·코스닥 지수 알려줘”(네이버 지수 API)

```bash
python3 scripts/korea_econ_api.py index --all
# 또는
python3 scripts/korea_econ_api.py index --market KOSPI
```

## 4) “코스피/코스닥, CPI, GDP 최신값 요약해줘”(ECOS 주요지표)

```bash
export ECOS_API_KEY="YOUR_KEY"
python3 scripts/korea_econ_api.py key-stats --filter "코스피|코스닥|국내총생산|소비자물가"
```

## 5) “GDP를 2010~2024 연간 시계열로 뽑아줘”

1) 먼저 ECOS에서 해당 지표의 `STAT_CODE`(및 필요 시 `ITEM_CODE`)를 확인한다.  
2) 아래처럼 실행한다.

```bash
export ECOS_API_KEY="YOUR_KEY"
python3 scripts/korea_econ_api.py ecos-series --stat-code 200Y101 --cycle A --start 2010 --end 2024
```

## 참고: 지수 vs ECOS

| 목적 | 권장 |
|------|------|
| 장중 지수·전일 종가·당일 OHLC 요약 | `index --market` / `index --all` |
| 한국은행 주요지표 카드 형태·공식 통계 스냅샷 | `key-stats --filter ...` |

