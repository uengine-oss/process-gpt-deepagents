# YouTube — Lite 커넥터 `pgpt-youtube`

계정 연결 없이도 도구가 보인다. API 자격 증명(사용자 금고의 API 키 → 서버 키 → Google 계정)이 있으면
YouTube Data API 로, 없으면 공개 페이지로 대신 읽는다.

| 도구 | 인자 | 하는 일 |
|---|---|---|
| `youtube_trending` | `region`(2자리, 기본 KR), `category`(카테고리 id — 1 영화·애니, 10 음악, 17 스포츠, 20 게임, 24 엔터테인먼트, 25 뉴스·정치, 27 교육, 28 과학·기술 …, 도구 설명의 목록에서 고른다), `max`(≤50) | 지역 인기 차트. API 가 없으면 「최근 24시간 업로드를 조회수 순」 근삿값(`exact_chart: false`) |
| `youtube_search` | `query`, `order`(viewCount·date·relevance), `publishedAfter`(`24h`·`7d` 처럼 시간·일 수, 서울 날짜 `YYYY-MM-DD` — 「오늘」은 오늘 날짜로, 또는 ISO 시각), `region`, `max` | 영상 검색. API 가 없으면 공개 검색 페이지 |
| `youtube_channel_videos` | `channel`(@핸들·채널 id·주소), `limit`(≤30) | 채널의 최근 업로드(RSS → yt-dlp → 채널 페이지) |
| `youtube_video_info` | `videoId` | 제목·게시일·길이·조회수·설명 |
| `youtube_transcript` | `videoId`, `lang`(기본 ko, 없으면 en) | 자막(수동·자동) 글 |

## 결과를 옮길 때

- `exact_chart: false` 나 `source: "public_search_page"` 면 정식 차트·API 결과가 아니다. 결과의 `basis`·`note` 를 밝히고,
  `tell_user`·`how_to_connect`·`api_missing` 을 그대로 전한다(설정 › 계정 › 연결 › YouTube 에 API 키 넣기 또는 Google 연결).
- 자막이 없는 영상은 `transcript: null` 이 온다 — 내용을 짐작해 요약하지 않는다.
- 이 도구가 아예 없으면 `youtube-public-data` 스킬의 공개 주소로 읽는다.
