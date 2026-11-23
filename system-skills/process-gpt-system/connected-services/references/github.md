# GitHub — Lite 커넥터 `pgpt-github`

| 도구 | 인자 | 하는 일 |
|---|---|---|
| `github_search_issues` | `query`(GitHub 검색 문법), `max`(≤50) | 이슈·PR 검색 |
| `github_list_prs` | `max`(≤50) | 사용자와 관련된 열린 PR(작성·담당·리뷰 요청) |
| `github_create_issue` | `repo`(`owner/name`), `title`, `body`, `labels` | 이슈 만들기 |

## 검색 문법

`repo:owner/name`, `org:이름`, `is:issue` / `is:pr`, `is:open` / `is:closed`, `label:bug`, `author:아이디`,
`assignee:@me`, `review-requested:@me`, `created:>=2026-11-01`, `updated:>2026-11-14`, 검색어.

## 요령

- 저장소 이름이 불분명하면 `github_search_issues` 로 `org:` 범위를 좁혀 후보를 찾고, 그래도 모르면 묻는다.
- 이슈를 만들 때 제목은 한 줄, 본문에 재현 순서·기대·실제를 나눠 쓴다. 라벨은 저장소에 있는 것만.
- 도구가 없고 공개 저장소면 `https://api.github.com/repos/<owner>/<name>/issues?state=open` 같은 공개 API 를
  샌드박스에서 읽을 수 있다(읽기만, 시간당 호출 수 제한이 낮다). 비공개 저장소는 커넥터 없이는 못 읽는다.
