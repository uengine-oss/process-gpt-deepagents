---
name: ieumoffice-live-edit
description: >
  메시지에 '사용자가 이음오피스 편집기에 문서를 열어 두었다'는 시스템 사실이 붙어 있고, 요청이 그 문서의
  내용을 읽거나 고치는 것일 때 쓴다(워드·엑셀·한글·슬라이드·PDF). 새 파일을 만들지 않고 사람이 보는
  화면에서 바로 표·셀·문단을 바꾸는 도구 사용법을 정한다. 열린 문서가 없거나, 열려 있어도 그 문서와
  상관없는 요청에는 쓰지 않는다.
metadata:
  version: 1.0.0
  include_tools: >-
    office_live_status office_live_documents office_live_read office_live_write office_live_replace
    office_live_apply_ops office_live_hwp_table office_live_hwp_text office_guide office_read_document
---

# 열린 문서를 그 화면에서 고치기

**이 도구들(`office_live_*`)이 목록에 없으면 이 스킬을 쓰지 않는다.** 스킬 전용 도구로 지정된 회사에서는 이 SKILL.md 를
읽은 다음 호출부터 도구가 보이니 그때 한 번 더 본다. 그래도 없으면 이음오피스가 없는 환경이다 — 3절 「연결되지 않았을 때」대로 한다.

## 1. 열린 문서 찾고 읽기

1. 메시지의 시스템 사실이 어떤 편집기(워드·엑셀·한글·슬라이드·PDF)에 어떤 파일이 열려 있는지 알려 준다.
2. `office_live_documents` 로 그 문서의 `session_id` 를 찾는다(파일 이름으로 맞춘다).
3. `office_live_read` 로 **지금** 내용을 읽는다(저장 전 편집도 들어 있다). 워드는 번호 붙은 블록 목록, 엑셀은 시트 요약
   (`addresses` 로 셀 값), 한글은 문단과 번호 붙은 표 목록이다.

## 2. 고치기 — 편집기마다

| 편집기 | 쓰는 도구 | 자세히 |
|---|---|---|
| 워드 | `office_live_apply_ops`(findReplace 먼저), `office_live_replace`, `office_live_write` | `references/docs.md` |
| 엑셀 | `office_live_apply_ops`(set_cell·set_formula·insert_rows …) | `references/sheets.md` |
| 한글 | `office_live_hwp_table`, `office_live_hwp_text` | `references/hwp.md` |
| 슬라이드·PDF | 화면에서 바로 고치는 도구가 없다 — `office_read_document` 로 읽어 답하고, 고칠 곳은 무엇을 어떻게 바꿀지 글로 알려 준다 | — |

공통 규칙:
- **사용자가 문서에서 가리킨 대상**(메시지 끝의 `@표 N`, `@이미지 N`, `@문단`, `@범위` 와 그 번호·행·열·범위)이 있으면 **그 대상만** 고친다.
- 고친 숫자에 딸린 합계·소계·비율이 있으면 다시 계산해 함께 고친다(엑셀은 수식으로).
- 이미 있는 서식(표 모양·글꼴·색)은 바꾸지 않는다. 바꾼 글은 이웃 글의 서식을 따른다.
- 편집 연산의 정확한 형식은 `office_guide`(format: docx·xlsx)로 확인할 수 있다.
- 그림 자체의 편집(색·자르기·합성)은 이 도구로 하지 않는다 — 이미지 편집 스킬(`photocraft-editing`)의 일이다.

## 3. 하지 않는 것

- **새 문서를 만들거나 파일로 내려받게 하지 않는다**(파일·다운로드 링크 금지). 고친 결과는 이미 사람의 화면에 있다.
- **저장하지 않는다.** 저장은 사용자가 편집기에서 한다(`office_live_save` 를 부르지 않는다).
- **연결되지 않았을 때**: `office_live_documents` 도구가 없거나 그 목록에 이 문서가 없으면 편집기와 연결되지 않은 것이다.
  새 문서를 만들지 말고 「편집기와 연결되지 않아 열린 문서를 바로 고치지 못했어요」라고 먼저 알린 뒤, 넣을 내용을 답 안에 글로만 보여 준다.

## 4. 끝맺기

다 고치면 무엇을 어디서 바꿨는지(블록·셀·표 번호) 짧게 답한다. 바꾸지 못한 부분이 있으면 이유와 함께 적는다.
