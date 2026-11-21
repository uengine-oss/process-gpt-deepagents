# 네이버 증권 공개 JSON (2026-11-21 확인)

키 없음. 헤더 `User-Agent: Mozilla/5.0` 을 넣는다(일부 주소는 없어도 되지만 넣는 것이 안전하다).
응답은 모두 JSON. PC판 `finance.naver.com` 은 쓰지 않는다.

## 1. 종목 검색(자동완성)

```
GET https://ac.stock.naver.com/ac?q=<검색어(URL 인코딩)>&target=stock,index
```

응답: `{"query": "...", "items": [ ... ]}` — 맞는 것이 없으면 `items` 가 빈 배열.

| 필드 | 뜻 | 예 |
|---|---|---|
| `code` | 종목 코드. 국내는 6자리(숫자만이 아닐 수 있다: `0162Z0`), 해외는 티커, 지수는 지수 코드 | `005930`, `TSLA`, `KOSPI`, `IXIC` |
| `name` | 표시 이름(한글) | |
| `typeCode` | 시장·종류 | `KOSPI` `KOSDAQ` `NASDAQ` `NYSE` `AMEX` `TOKYO` `HONG_KONG` `INDEX`(국내 지수) `WORLD_INDEX` `FUTURES` `WORLD_FUTURES` |
| `typeName` | 시장 이름(한글) | |
| `reutersCode` | **해외 종목·해외 지수 조회에 쓰는 코드**. 국내는 `code` 와 같다 | `TSLA.O`, `7203.T`, `0700.HK`, `.IXIC`, `.INX`, `.DJI`, `.N225` |
| `nationCode` | 나라(지수는 `null`) | `KOR` `USA` `JPN` `HKG` … |
| `category` | `stock`(ETF 포함) / `index`(선물 포함) | |
| `url` | 네이버 증권 화면 경로(출처 줄에 써도 된다) | `/worldstock/stock/TSLA.O/total` |

알아 둘 것(확인한 예):
- 지수 이름이 회사 이름과 겹치면 회사가 먼저 나온다. 「다우」는 Dow Inc.(NYSE 종목)가 먼저이고,
  다우존스 지수는 「다우존스」(`.DJI`, WORLD_INDEX)로 찾힌다. `category` 로 가른다.
- 「나스닥」은 나스닥 거래소 운영사 종목(`NDAQ`)과 「나스닥 종합」(`.IXIC`)·「나스닥 100」(`.NDX`)이 함께 나온다.
- 일본 종목은 ADR(NYSE)과 도쿄 원주(`7203.T`)가 같이 나온다.
- 영문 검색어도 된다(`tesla` → 테슬라 `TSLA.O`).

## 2. 현재 가격(기본 정보)

| 대상 | 주소 |
|---|---|
| 국내 종목·ETF | `https://m.stock.naver.com/api/stock/<code>/basic` |
| 국내 지수 | `https://m.stock.naver.com/api/index/<code>/basic` (`KOSPI`, `KOSDAQ`, `KPI200` …) |
| 해외 종목 | `https://api.stock.naver.com/stock/<reutersCode>/basic` |
| 해외 지수 | `https://api.stock.naver.com/index/<reutersCode>/basic` (`.IXIC`, `.INX`, `.DJI`, `.N225` …) |

주요 필드(값은 **쉼표가 든 문자열**이다 — 숫자로 바꿀 때 쉼표를 뺀다):

| 필드 | 뜻 |
|---|---|
| `stockName` / `indexName` | 이름(해외 지수는 `indexName`) |
| `closePrice` | 현재가(장중) 또는 마지막 종가, 예 `"180,900"` |
| `compareToPreviousClosePrice` | 전일 대비 금액, 예 `"-16,000"` |
| `fluctuationsRatio` | 전일 대비 %, 예 `"-8.13"` |
| `compareToPreviousPrice.name` | `RISING` / `FALLING` / `EVEN` 등 |
| `marketStatus` | `OPEN`(장중) / `CLOSE` |
| `localTradedAt` | 값의 기준 시각(그 시장 시간대 ISO), 예 `2026-11-20T16:00:00-05:00` |
| `stockExchangeType` | 거래소 정보(`name`, `zoneId`, `startTime`, `endTime`) |

틀린 코드는 HTTP 409 `{"code":"StockConflict"}`.

## 3. 일봉·주봉·월봉

| 대상 | 주소 |
|---|---|
| 국내 종목·ETF | `https://api.stock.naver.com/chart/domestic/item/<code>/day` |
| 국내 지수 | `https://api.stock.naver.com/chart/domestic/index/<code>/day` |
| 해외 종목 | `https://api.stock.naver.com/chart/foreign/item/<reutersCode>/day` |
| 해외 지수 | `https://api.stock.naver.com/chart/foreign/index/<reutersCode>/day` |

- 쿼리: `?startDateTime=YYYYMMDD0000&endDateTime=YYYYMMDD0000` (`YYYYMMDD` 만 줘도 된다). 끝 날짜를 포함한다.
- `day` 대신 `week`·`month` 를 쓰면 주봉·월봉(행의 `localDate` 는 그 주·달의 시작일).
- 응답: 오래된 날부터의 배열. **거래일만** 있다(주말·휴장일 없음). 맞는 행이 없거나 코드가 틀리면 `[]`.

| 필드 | 뜻 |
|---|---|
| `localDate` | `YYYYMMDD`(그 시장의 날짜) |
| `closePrice` · `openPrice` · `highPrice` · `lowPrice` | 숫자(float) |
| `accumulatedTradingVolume` | 거래량 |
| `foreignRetentionRate` | 외국인 보유율(국내만) |

## 4. 예시(샌드박스 python)

```python
import json, urllib.parse, urllib.request
from datetime import date, timedelta

UA = {"User-Agent": "Mozilla/5.0"}

def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))

def search(name):
    q = urllib.parse.quote(name)
    return get(f"https://ac.stock.naver.com/ac?q={q}&target=stock,index")["items"]

def daily(item, days=40):
    end = date.today(); start = end - timedelta(days=days)
    rng = f"startDateTime={start:%Y%m%d}0000&endDateTime={end:%Y%m%d}0000"
    kind = "index" if item["category"] == "index" else "item"
    where = "domestic" if item["nationCode"] == "KOR" or item["typeCode"] == "INDEX" else "foreign"
    code = item["code"] if where == "domestic" else item["reutersCode"]
    return get(f"https://api.stock.naver.com/chart/{where}/{kind}/{code}/day?{rng}")

def change_over(rows, n):
    """n 거래일 누적 등락률(%), 기준이 된 두 행"""
    if len(rows) < n + 1:
        return None, None, None
    last, base = rows[-1], rows[-1 - n]
    return (last["closePrice"] / base["closePrice"] - 1) * 100, base, last
```

국내 지수의 `nationCode` 는 `null` 이고 `typeCode` 가 `INDEX` 다(위 `where` 판정).
선물(`FUTURES`·`WORLD_FUTURES`)은 이 주소들로 확인하지 않았다 — 물으면 조회가 되는지 먼저 보고, 안 되면 그렇다고 쓴다.

## 출처 줄

`출처: 네이버 증권(https://api.stock.naver.com/chart/foreign/item/TSLA.O/day)` 처럼 실제로 읽은 주소를 적는다.
여러 종목이면 주소를 쉼표로 잇거나 `네이버 증권 공개 시세(종목 검색·일봉)` 처럼 묶어 쓴다.
