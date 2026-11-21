# Notion — Lite 커넥터 `pgpt-notion`

| 도구 | 인자 | 하는 일 |
|---|---|---|
| `notion_search` | `query` | 통합(integration)에 공유된 페이지 검색 |
| `notion_read_page` | `pageId` | 페이지 글 읽기 |
| `notion_create_page` | `parentPageId`, `title`, `markdown` | 상위 페이지 아래 새 페이지 |
| `notion_append_to_page` | `pageId`, `markdown` | 페이지 끝에 덧붙이기 |

## 요령

- Notion 은 **통합에 공유된 페이지만** 보인다. 찾는 페이지가 검색에 없으면 「그 페이지를 Notion 에서 통합에
  공유(연결 추가)해야 보인다」고 알린다 — 없는 페이지라고 단정하지 않는다.
- 새 페이지는 상위 페이지 id 가 필요하다. `notion_search` 로 상위 페이지를 찾고, 여럿이면 묻는다.
- `markdown` 은 제목(#)·목록(-)·문단 정도의 간단한 형식만 옮겨진다. 표가 필요하면 목록으로 풀어 쓴다.
- 기존 페이지를 고쳐 달라는 요청은 덧붙이기만 된다 — 덧붙였다고 정확히 알린다.
