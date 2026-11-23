---
name: youtube-public-data
description: >
  유튜브의 공개 정보 — 채널의 최근 업로드, 주제·검색어별 최근 영상과 조회수 순위, 영상 한 개의
  조회수·게시일 — 를 읽어 올 때 쓴다. 유튜브 전용 도구가 없거나 API 키·계정 연결 문제로 실패해도
  공개 피드와 검색 결과 페이지에서 구하는 방법, 공개로는 볼 수 없는 것(지역 인기 차트)을 정한다.
  영상 편집·업로드, 영상 내용 요약에는 쓰지 않는다.
metadata:
  version: 1.0.0
  include_tools: youtube_trending youtube_search youtube_channel_videos youtube_video_info youtube_transcript
---

# 유튜브 공개 정보 읽기

## 0. 도구가 있으면 도구 먼저

도구 목록에 `youtube_` 도구가 있으면 그것부터 쓴다 — 채널 업로드 `youtube_channel_videos`,
검색 `youtube_search`, 영상 정보 `youtube_video_info`, 지역 인기 차트 `youtube_trending`.
이 도구들은 API 키가 없으면 스스로 공개 페이지로 대신 읽고, 무엇이 빠졌는지(`api_missing`·`missing`,
`how_to_connect`, `tell_user`)를 결과에 담아 돌려준다. 그 값은 줄이거나 바꾸지 말고 답에 옮긴다.

도구가 없거나 오류로 끝났을 때만 아래 1~4 를 샌드박스(`execute`)에서 직접 한다. 주소·파라미터·
응답 위치·예시 코드는 `references/feed-and-search.md` 에 있다 — 직접 읽기 전에 그 파일을 읽는다.

## 1. 채널 → 채널 id

- 채널 id 는 `UC` 로 시작하는 24자다. 주소나 말에 있으면 그대로 쓴다.
- `@핸들`·채널 주소·채널 이름만 있으면 채널 페이지(`https://www.youtube.com/@핸들`) HTML 에서 찾는다:
  `<link rel="canonical" href="https://www.youtube.com/channel/UC…">` 또는 `"externalId":"UC…"`.
- 이름만 있고 핸들을 모르면 검색 결과(3절)의 채널 링크에서 id 를 얻는다. 비슷한 채널이 여럿이면 후보를 보이고 고른 근거를 쓴다.

## 2. 채널의 최근 업로드

채널 RSS 피드(`feeds/videos.xml?channel_id=UC…`) — 새 영상 15개, 키 없이 읽힌다. 영상마다
id·제목·게시 시각·조회수(`media:statistics views`)·좋아요 수가 있다. 15개보다 많이 필요하면
채널의 동영상 탭 페이지(`/@핸들/videos`)의 `ytInitialData` 를 읽는다.

## 3. 주제·검색어로 찾기

검색 결과 페이지(`results?search_query=…&sp=…`)의 HTML 안 `ytInitialData` JSON 에서 영상을 꺼낸다.
`sp` 파라미터가 정렬(관련성·최신·조회수)과 올린 날짜(지난 1시간·오늘·이번 주·이번 달·올해) 거르기를
정한다 — 값 표는 references 에 있다. 결과의 영상 항목은 `videoRenderer`(지금 검색 화면) 또는
`lockupViewModel`(새 배치)로 온다.

- 날짜 거르기는 느슨하다(오늘 거르기에도 며칠 전 영상이 섞인다). 「오늘」「이번 주」가 중요하면
  영상마다 `publishedTimeText`(「20시간 전」「3 days ago」)로 다시 거른다.
- 조회수는 글자(「조회수 1,536,759회」「1.2만회」「1.2M views」)라 숫자로 바꿔 정렬한다.

## 4. 영상 한 개의 정보

`https://www.youtube.com/watch?v=<id>` HTML 의 `ytInitialPlayerResponse` 에서 `videoDetails`
(제목·채널·`viewCount`·`lengthSeconds`·설명)와 `microformat.playerMicroformatRenderer.publishDate` 를 읽는다.

## 5. 공개로 볼 수 없는 것

- **지역 인기 차트(인기 급상승)** 는 YouTube Data API(`youtube_trending`)로만 볼 수 있다. 공개 인기 페이지는
  2025년에 없어졌고, `https://www.youtube.com/feed/trending` 은 지금 영상 없이 홈 화면 껍데기만 돌려준다
  (2026-11-21 확인). 브라우저로 찾으러 가지 않는다.
- 차트를 물었는데 API 를 못 쓰면: 최근 24시간에 올라온 영상을 넓은 주제(뉴스·음악·게임·예능·스포츠 등)로
  검색해 조회수 순으로 모은 **근삿값**을 줄 수 있다. 이때 「정식 인기 차트가 아니라 근삿값」이라고 밝히고,
  정확한 차트에는 YouTube API 키나 Google 계정 연결이 필요하다고 함께 적는다.
- 비공개·일부 공개 영상, 채널 구독자 목록, 시청 기록, 수익 같은 계정 정보는 공개 자료에 없다 — 연결된 도구가 없으면 못 한다고 쓴다.

## 6. 답에 적을 것

- 영상마다 제목 · 채널 · 조회수(읽은 그대로의 글자도 함께) · 게시 시각 · 주소.
- 어디서 읽었는지: 「유튜브 채널 RSS」「유튜브 공개 검색 결과 페이지(조회 시각)」 또는 도구 이름.
- 공개 페이지에서 읽은 조회수는 그 순간의 표시값이고 좋아요 수는 없다는 점.
