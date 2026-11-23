# 유튜브 공개 주소·파라미터·응답 위치 (2026-11-21 확인)

## 요청 헤더

```
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36
Accept-Language: ko-KR,ko;q=0.9,en;q=0.6      (영어 결과가 필요하면 en-US 먼저)
Cookie: CONSENT=YES+1; SOCS=CAI                (유럽 동의 화면을 건너뛴다)
```

## 채널 RSS 피드

```
GET https://www.youtube.com/feeds/videos.xml?channel_id=UC<22자>
```

Atom XML, 새 영상 15개. `<feed><title>` 이 채널 이름. `<entry>` 마다:

| 요소 | 값 |
|---|---|
| `yt:videoId` | 영상 id (`https://www.youtube.com/watch?v=<id>`) |
| `title` | 제목 |
| `published` | 게시 시각(UTC ISO) |
| `media:group/media:statistics@views` | 조회수 |
| `media:group/media:community/media:starRating@count` | 좋아요 수 |
| `media:group/media:thumbnail@url` | 썸네일 |
| `media:group/media:description` | 설명 |

채널 id 가 틀리면 404.

## 채널 id 찾기

`https://www.youtube.com/@<핸들>` HTML 에서:
- `<link rel="canonical" href="https://www.youtube.com/channel/(UC[\w-]{22})">`
- 또는 `"externalId":"(UC[\w-]{22})"`, `"channelId":"(UC[\w-]{22})"`
- 채널 이름: `<meta property="og:title" content="…">`

## 검색 결과 페이지

```
GET https://www.youtube.com/results?search_query=<검색어>&sp=<필터>&hl=ko&gl=KR
```

`sp` 값(동영상만 보기 포함). 정렬 × 올린 날짜:

| 정렬 \ 올린 날짜 | 전체 | 지난 1시간 | 오늘 | 이번 주 | 이번 달 | 올해 |
|---|---|---|---|---|---|---|
| 관련성 | `EgIQAQ%3D%3D` | `EgQIARAB` | `EgQIAhAB` | `EgQIAxAB` | `EgQIBBAB` | `EgQIBRAB` |
| 최신순 | `CAISAhAB` | `CAISBAgBEAE%3D` | `CAISBAgCEAE%3D` | `CAISBAgDEAE%3D` | `CAISBAgEEAE%3D` | `CAISBAgFEAE%3D` |
| 조회수순 | `CAMSAhAB` | `CAMSBAgBEAE%3D` | `CAMSBAgCEAE%3D` | `CAMSBAgDEAE%3D` | `CAMSBAgEEAE%3D` | `CAMSBAgFEAE%3D` |

(`sp` 는 작은 protobuf 를 base64 한 것이다: 정렬 `08 0n`(관련성 0·평점 1·최신 2·조회수 3, 관련성은 생략),
필터 `12 len [08 날짜(1~5)] 10 01`(10 01 = 동영상만). URL 에 넣을 때 `=` 은 `%3D`.)

HTML 안 `var ytInitialData = {…};` 를 꺼낸다(문자열 안의 중괄호를 건너뛰는 괄호 맞추기, 또는
python `json.JSONDecoder().raw_decode(html[i:])`). 영상 목록 위치:

```
contents.twoColumnSearchResultsRenderer.primaryContents.sectionListRenderer
  .contents[*].itemSectionRenderer.contents[*].videoRenderer
```

배치가 바뀌어도 되도록 JSON 전체를 훑어 `videoRenderer`(키) 와 `lockupViewModel`(`contentType` 이 VIDEO)을 모은다.

`videoRenderer` 필드(글은 `{"simpleText": …}` 또는 `{"runs": [{"text": …}]}`):

| 필드 | 값 |
|---|---|
| `videoId` | id |
| `title` | 제목 |
| `ownerText` / `longBylineText` | 채널 이름, `runs[0].navigationEndpoint.browseEndpoint.browseId` = 채널 id |
| `viewCountText` / `shortViewCountText` | 「조회수 1,536,759회」「153만회」 |
| `publishedTimeText` | 「20시간 전」「4일 전」 |
| `lengthText` | 「5:14」 |
| `thumbnail.thumbnails[-1].url` | 썸네일 |

`lockupViewModel`: id 는 `contentId`, 제목 `metadata.lockupMetadataViewModel.title`, 조회수·게시 시각은
`metadata.lockupMetadataViewModel.metadata.contentMetadataViewModel.metadataRows[*].metadataParts[*]`
의 `text`(또는 `accessibilityLabel` 「조회수 193만회」), 길이는 `contentImage` 안의 「m:ss」 배지.

조회수 글자 → 숫자: 쉼표 빼기, 천=1e3·만=1e4·억=1e8, K=1e3·M=1e6·B=1e9, 「조회수 없음」/「No views」=0.

## 영상 페이지

```
GET https://www.youtube.com/watch?v=<id>&hl=ko&gl=KR
```

`var ytInitialPlayerResponse = {…};` 의
- `videoDetails`: `videoId`, `title`, `author`, `channelId`, `viewCount`(문자열 숫자), `lengthSeconds`, `shortDescription`
- `microformat.playerMicroformatRenderer.publishDate`(`YYYY-MM-DD…`)

## 인기 차트

- API 만: `GET https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics&chart=mostPopular&regionCode=KR&maxResults=10&key=<키>` (키가 있을 때만).
- `https://www.youtube.com/feed/trending` 은 2026-11-21 에 영상 없이 홈 화면(`FEwhat_to_watch`)만 돌려줬다.

## 예시(샌드박스 python)

```python
import json, re, urllib.parse, urllib.request

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
     "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6", "Cookie": "CONSENT=YES+1; SOCS=CAI"}

def page(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=20) as r:
        return r.read().decode("utf-8", "replace")

def json_after(html, marker):
    i = html.find(marker)
    if i < 0:
        return None
    return json.JSONDecoder().raw_decode(html[html.index("{", i):])[0]

def text(x):
    if not x: return None
    return x.get("simpleText") or "".join(r.get("text", "") for r in x.get("runs", []))

def videos(data):
    out = []
    def walk(o):
        if isinstance(o, dict):
            v = o.get("videoRenderer")
            if v and v.get("videoId"):
                out.append({"id": v["videoId"], "title": text(v.get("title")), "channel": text(v.get("ownerText")),
                            "views": text(v.get("viewCountText")), "published": text(v.get("publishedTimeText")),
                            "length": text(v.get("lengthText"))})
                return
            for x in o.values(): walk(x)
        elif isinstance(o, list):
            for x in o: walk(x)
    walk(data)
    return out

def search(query, sp="CAMSBAgCEAE%3D"):          # 조회수순 · 오늘
    url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}&sp={sp}&hl=ko&gl=KR"
    return videos(json_after(page(url), "var ytInitialData = "))

def channel_id(handle):
    html = page(f"https://www.youtube.com/@{urllib.parse.quote(handle.lstrip('@'))}")
    m = re.search(r'"externalId":"(UC[\w-]{22})"', html)
    return m and m.group(1)
```
