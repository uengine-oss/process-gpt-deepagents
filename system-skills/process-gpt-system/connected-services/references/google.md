# Google (Gmail · 캘린더 · Drive) — Lite 커넥터 `pgpt-google`

사용자가 Lite 설정 › 계정 › 연결에서 Google 을 연결했을 때만 도구가 목록에 나온다. 사용자 본인의 토큰으로 돈다.

| 도구 | 인자 | 하는 일 |
|---|---|---|
| `gmail_search` | `query`(Gmail 검색 문법), `max`(≤30) | 보낸 사람·제목·날짜·미리 보기 목록 |
| `gmail_read` | `id` | 메일 한 통의 본문(일반 글) |
| `gmail_draft` | `to`, `subject`, `body` | 초안만 만든다(보내지 않음). 사용자가 검토해 보낸다 |
| `calendar_find_events` | `timeMin`, `timeMax`(ISO 날짜·시각), `q`(선택) | 기간 안의 일정 |
| `calendar_create_event` | `summary`, `start`, `durationMin`, `description`, `location` | 일정 만들기 |
| `calendar_update_event` | `eventId` + 바꿀 필드 | 일정 고치기 |
| `drive_search` | `query`, `max`(≤30) | Drive 전문 검색 |
| `drive_read` | `fileId` | 파일을 글로(문서 → 텍스트, 시트 → CSV) |

## 인자 요령

- Gmail 검색 문법: `from:이름@도메인`, `to:`, `subject:`, `newer_than:7d`, `older_than:1m`, `after:2026/11/01`,
  `before:`, `is:unread`, `has:attachment`, `filename:pdf`, `label:`, 공백 = 그리고, `OR`, `-` = 제외.
  찾은 목록의 `id` 로 `gmail_read` 를 부른다.
- 캘린더 `start`: 종일 일정은 `YYYY-MM-DD`, 시각이 있으면 시간대 오프셋을 붙인 ISO(`2026-11-24T14:00:00+09:00`).
  「다음 주 화요일」처럼 말했으면 오늘 날짜(시스템이 알려 준 현재 시각)로 날짜를 계산하고 답에 날짜를 적는다.
- 일정을 만들기 전에 `calendar_find_events` 로 그 시간대를 먼저 본다(겹침·중복 방지).
- Drive 는 `drive_search` 로 `fileId` 를 얻은 뒤 `drive_read`.

## 실패

- 「Google (Gmail·캘린더·Drive) 이(가) 연결되지 않았어요. 설정 → 계정 → 연결에서 연결한 뒤 다시 시도하세요.」 — 그대로 전한다.
- 권한(범위)이 모자라면 도구 오류에 어떤 권한인지가 온다 — 그대로 전하고 「다시 연결(권한 넓히기)」을 안내한다.
