## Why

deepagent 의 BPMN 프로세스 생성 흐름이 (a) 모드 분기·초심자/숙련자 분기로 일관성이 없고, (b) 에이전트가 DB 저장·스킬 서비스 업로드·workspace 자동 삭제까지 수행해 보안·예측가능성 문제가 있으며, (c) 생성 결과를 사용자가 프론트에서 일관되게 미리 볼 UI 연결이 부족하다. 에이전트는 **산출물 생성까지만** 하고, 확인·저장은 사용자/프론트가 담당하도록 단일화한다.

## What Changes

- **요청 형태 무관 일관 생성**: 단일/다중 문서, 단일/다중 자연어 프로세스 요청 모두 동일한 단계 흐름으로 처리(다중이면 프로세스별 반복).
- **초심자/숙련자 모드 제거** (BREAKING): orientation/모드 분기 문구·분기 로직 삭제. 항상 동일하게 동작.
- **저장·업로드·정리 제거** (BREAKING):
  - 스킬 서비스 업로드 도구(`upload_skill`)·`CLAUDE_SKILLS_BASE_URL` 의존 제거 — 스킬은 **workspace 내 산출물 파일**로만 둔다.
  - `complete_process_generation` 의 **workspace 자동 삭제 제거** — 산출물 파일을 보존한다.
  - 에이전트는 DB 에 쓰지 않고(읽기 전용), 스킬/프로세스 저장도 하지 않는다(저장은 사용자가 프론트에서).
- **산출물 표시 연결**: 생성 결과(process-definition + forms + skills + agents)를 프론트 **ArtifactPanel**(우측 산출물 사이드바)에 띄워 사용자가 미리보고 저장 결정.

## Capabilities

### New Capabilities
- `bpmn-process-generation`: deepagent 가 요청(문서/자연어, 단일/다중)에 맞춰 BPMN 산출물(process-definition.json·forms·skills·agents)을 **샌드박스 workspace 파일로만** 생성한다(저장·업로드·삭제 없음, 모드 분기 없음, 검증 자동개선 포함).
- `bpmn-artifact-display`: 프론트(process-gpt-vue3)가 생성된 BPMN 산출물을 ArtifactPanel 에 표시하고(프로세스 미리보기 + 스킬/에이전트 목록), 사용자가 '저장' 으로 확정한다.

### Modified Capabilities
<!-- 기존 openspec spec 없음(첫 도입) — 모두 신규 capability 로 등록 -->

## Impact

- **백엔드(process-gpt-deepagents)**: `core/skills/tools.py`(`upload_skill` 제거, `complete_process_generation` 정리·삭제 제거), `core/agents/agent.py`(시스템 프롬프트 단일화), 스킬 문서(`skills/.../SKILL.md`, `references/00,03,04,09,11`).
- **프론트(process-gpt-vue3)**: `ArtifactPanel.vue`(+ BPMN 산출물 뷰어), `ChatRoomPage.vue`(`onProcessResult` → `pushArtifactPanel` 연결, 저장 버튼).
- **테스트/문서**: playwright e2e 테스트 + 실행 리포트 + 사용 매뉴얼.
- 환경변수 `CLAUDE_SKILLS_BASE_URL` 더 이상 사용 안 함.
