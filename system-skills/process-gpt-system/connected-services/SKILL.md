---
name: connected-services
description: >
  메일함·캘린더·클라우드 드라이브·팀 메신저·코드 저장소·문서/위키 도구 같은 바깥 업무 서비스(SaaS)에서
  사용자의 정보를 읽거나 거기에 글·일정·이슈·페이지를 만들 때, 그 서비스의 도구를 부르거나 다른 방법을
  찾기 전에 먼저 읽는다(도구가 목록에 있든 없든). 연결 도구 → 공개 자료 → 내장 브라우저(읽기만) 순서,
  서비스별 인자 요령, 끝내 못 했을 때 빠진 연결과 연결하는 곳을 그대로 알리는 법을 정한다.
  이 플랫폼 안의 프로세스·문서 작업과 일반 웹 검색에는 쓰지 않는다.
metadata:
  version: 1.0.0
  include_tools: >-
    calendar_find_events calendar_create_event calendar_update_event gmail_search gmail_read gmail_draft
    drive_search drive_read slack_list_channels slack_read_channel slack_post_message
    github_search_issues github_list_prs github_create_issue
    notion_search notion_read_page notion_create_page notion_append_to_page
    youtube_trending youtube_search youtube_channel_videos youtube_video_info youtube_transcript
    browser_task
---

# 바깥 서비스 쓰기

아래 도구 이름은 Process GPT Lite 의 커넥터 기준이다(`references/<서비스>.md`). 회사에 따라 다른 이름의
커넥터가 붙어 있을 수 있으니, 이름보다 **도구 설명이 그 서비스를 다루는지**로 맞춘다. 도구가 하나도 없어도
이 절차를 따른다 — 2절이 그때 무엇을 알릴지 정한다.

| 서비스 | 도구(Lite) | 자세히 |
|---|---|---|
| Gmail · Google 캘린더 · Google Drive | `gmail_*` `calendar_*` `drive_*` | `references/google.md` |
| Slack | `slack_*` | `references/slack.md` |
| GitHub | `github_*` | `references/github.md` |
| Notion | `notion_*` | `references/notion.md` |
| YouTube | `youtube_*` (계정 연결 없이도 동작) | `references/youtube.md`, 공개 자료는 `youtube-public-data` 스킬 |
| 그 밖의 사이트(로그인 필요한 곳 포함) | `browser_task`(읽기만) | 아래 1-③ |

## 1. 시도 순서

① **연결 도구 먼저.** 그 서비스 도구가 목록에 있으면 웹 조회(`fetch_webpage`)나 브라우저보다 먼저 쓴다.
   쓰기 전에 그 서비스의 references 파일에서 인자 형식을 확인한다(날짜 형식, 검색 문법, id 를 얻는 순서).

② **도구가 없거나 실패하면 공개 자료.** 그 서비스가 로그인 없이 내주는 것만 샌드박스에서 직접 읽는다 —
   유튜브는 채널 RSS·공개 검색 페이지(`youtube-public-data` 스킬), 공개 GitHub 저장소는 `https://api.github.com`
   의 공개 API, 공개된 Notion·Drive 문서는 그 공유 주소. 개인 메일함·일정·비공개 채널은 공개 자료가 없다.

③ **그래도 안 되면 내장 브라우저.** `browser_task` 가 있으면 `goal`(무엇을 읽어 올지)과 `start_url`(공개 웹 주소만)로
   부른다. 사용자 이름으로 로그인해 **읽기만** 한다 — 글 올리기·보내기·지우기·결제·설정 바꾸기는 하지 않는다.
   몇 분 걸린다. 쓰기가 필요한 일이면 브라우저로 대신하지 않고 2절로 간다.

④ **모두 안 되면 2절**대로 알린다.

## 2. 못 했을 때 알릴 것

"연결되어 있지 않다"로 끝내지 않는다. **무엇이 없어서 못 했는지와 어디서 연결하는지**를 쓴다.

- 도구가 돌려준 `missing`·`api_missing`·`how_to_connect`·`tell_user`·`admin` 값이 있으면 **그대로 옮긴다**(줄이거나 바꾸지 않는다).
- 도구 오류가 「연결되지 않았어요 … 연결한 뒤 다시 시도하세요」 꼴이면, 그 서비스 이름과 오류에 적힌 연결 위치를 그대로 쓴다.
- 그 서비스 도구가 **목록에 아예 없으면**: 「이 회사 에이전트에 <서비스> 커넥터가 등록되어 있지 않아 관리자가 등록해야 해요」
  라고 쓴다. Lite 의 커넥터 서버 이름은 `pgpt-google`·`pgpt-slack`·`pgpt-github`·`pgpt-notion`·`pgpt-youtube`·`pgpt-browser` 다.
  Lite 커넥터가 없는 서비스(예: 사내 그룹웨어, 다른 SaaS)는 「이 서비스용 커넥터가 없다」고 쓰고, 사용자가 직접 내보낸
  파일을 첨부하면 그것으로 할 수 있다고 덧붙인다.
- 무엇을 시도했는지(도구 → 공개 자료 → 브라우저 중 어디까지, 각각의 실패 이유) 한 줄씩 적는다.
- 못 읽은 메일·일정·메시지·이슈의 내용을 추측해 채우지 않는다.

## 3. 바깥에 남는 일(쓰기)

메일 초안, 일정 만들기·바꾸기, 메신저 게시, 이슈·페이지 만들기는 사용자 이름으로 바깥에 남는다.

- 요청에 대상(채널·저장소·상위 페이지·받는 사람)과 내용이 분명하면 바로 한다. 빠졌으면 묻는다
  (`request_human_input` 이 있으면 그것으로, 후보 목록을 함께).
- 여러 사람이 보는 곳(공유 채널, 팀 저장소)에 올리기 전에는 올릴 글을 보여 주고 확인을 받는다.
- 메일은 **보내지 않는다** — `gmail_draft` 로 초안만 만들고 사용자가 보낸다고 알린다.
- 일정은 만들기 전에 같은 시간대의 기존 일정을 찾아 겹치는지 본다.
- 사람이 없는 실행(예약 실행)에서는 요청에 적힌 쓰기만 하고, 그 밖에는 읽기만 한다.
- 끝나면 만든 것의 주소·id 를 답에 적는다.

## 4. 결과를 쓸 때

- 어느 서비스의 무엇에서 읽었는지(도구 이름 또는 주소)와 기간·검색 조건을 밝힌다.
- 개인 정보(메일 본문, 다른 사람의 연락처)는 요청에 필요한 만큼만 옮긴다.
