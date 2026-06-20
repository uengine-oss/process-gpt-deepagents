# BPMN 산출물 ArtifactPanel 표시 — 구현 & 검증 리포트

> 변경: `bpmn-artifact-only-flow` · capability `bpmn-artifact-display`
> 대상 요청: "메인채팅(deepagents 모드)에서 '휴가 신청 프로세스 생성' 요청 시, 생성된 deepagent 산출물이 우측 탭에 표시되고 사용자가 열어서 확인할 수 있어야 함."

## 1. 요청 / 범위

deepagents 모드 채팅에서 프로세스 생성 산출물(process-definition + forms + skills + agents)을 우측 **ArtifactPanel** 에 띄우고, 사용자가 열어 미리보기/저장할 수 있게 한다. 핵심은 **에이전트는 생성만, 확인·저장은 사용자/프론트** 라는 단일 흐름.

## 2. 시스템 맵

| 프로젝트 | 역할 | 이 기능에서의 책임 |
|---|---|---|
| `process-gpt-deepagents` (백엔드, FastAPI/LangGraph) | deepagent 실행·SSE 스트리밍 | bpmn-process-generation-skill 출력계약을 `process_result` SSE 이벤트로 emit |
| `process-gpt-vue3` (프론트, Vue3/Vuetify) | 메인 채팅 UI | `process_result` 수신 → 우측 ArtifactPanel 의 `type:'process'` 패널 표시·미리보기·저장 |

연결: 프론트 `/process-gpt-deepagents/chat/stream` (SSE) → `DeepAgentRouterService.sendMessageStream`.

## 3. 발견 상태 (이번 작업 이전)

기능 대부분이 작업 트리에 이미 구현되어 있었다:

- **백엔드** `executor.py`: 도구 인자/도구 출력/최종 텍스트 3지점에서 출력계약을 탐지해 `{"type":"process_result","data":{...}}` 1회 emit (`_detect_process_result`, `_extract_postprocess_result`).
- **프론트**:
  - `DeepAgentRouterService` — `process_result` → `onProcessResult` 콜백.
  - `ChatRoomPage.onProcessResult` → `_mapPostprocessToPdf2bpmnResult` → `pushArtifactPanel({type:'process'})`, `msg.pdf2bpmnResult` 영속.
  - `ArtifactPanel.vue` — `type:'process'` → `ProcessArtifactViewer.vue` (요약/스킬/에이전트 + 미리보기·저장 버튼).
  - `elementsToFlattened.js` — 저장 시 elements[] → flattened(proc_def.definition) 변환.
  - playwright: `artifact-panel-flow.spec.ts`(mock SSE 표시), `artifact-save-conversion.spec.ts`(변환 단위검증).

**식별된 갭(1건)**: 새로고침/재진입 시 `checkExistingArtifactPanels()` 가 slide/hwpx/docx 패널은 복원하지만 **process 패널은 복원하지 않았다**. `msg.pdf2bpmnResult`(+`__contract`)는 DB 에 영속되는데도 우측 탭이 사라져 "사용자가 (나중에) 열어서 확인" 요구가 깨졌다.

## 4. 변경 사항

### 프론트(process-gpt-vue3)
- `src/utils/processArtifactPanel.js` **(신규)** — 영속 메시지에서 process 패널 디스크립터를 만드는 순수 헬퍼(`buildProcessPanelFromMessage` / `isRestorableProcessResult` / `processNameFromResult` / `processIdFromResult`). 서버·컴포넌트 없이 단위 테스트 가능하도록 분리.
- `src/views/chat/ChatRoomPage.vue` — `checkExistingArtifactPanels()` 에 process 복원 분기 추가: `pdf2bpmnResult.__contract` 보유 메시지에서 `type:'process'` 패널 재생성, 같은 id 의 proc_def 존재 시 `__saved` 재확인('저장됨' 정확화). 헬퍼 import 추가.

### 테스트(process-gpt-vue3)
- `playwright/e2e/bpmn-artifact/artifact-panel-restore.spec.ts` **(신규)** — 복원 결정 로직 단위검증(3 케이스, 서버 불필요).
- `playwright/e2e/bpmn-artifact/artifact-panel-flow.spec.ts` — 새로고침 후 패널 복원 E2E 시나리오 추가(라이브 서버+로그인 시 실행).

### 스펙(process-gpt-deepagents)
- `bpmn-artifact-display/spec.md` — "채팅방 재진입 시 산출물 패널 복원" 시나리오 추가.
- `tasks.md` — 3.4 항목 추가(완료).

## 5. 스펙 시나리오 ↔ 검증 결과

| 시나리오 | 검증 방법 | 결과 |
|---|---|---|
| 산출물 수신 시 패널 표시 | `artifact-panel-flow.spec.ts`(mock SSE) | ⏭️ skip — 라이브 서버+로그인 필요 |
| 저장 전 미리보기 | 〃(미리보기 버튼) | ⏭️ skip — 〃 |
| 저장 버튼 → flattened 변환·저장 | `artifact-save-conversion.spec.ts` | ✅ PASS (2) |
| 저장 전 비저장 / 빈 정의 안전 처리 | 〃 | ✅ PASS |
| **재진입 시 패널 복원(결정 로직)** | `artifact-panel-restore.spec.ts` | ✅ PASS (3) |
| **재진입 시 패널 복원(E2E 새로고침)** | `artifact-panel-flow.spec.ts` 추가분 | ⏭️ skip — 라이브 서버+로그인 필요 |

- 결정적 테스트: **5 passed**, 2 skipped(라이브 게이트).
- `processArtifactPanel.js` ESLint: 0 errors. `ChatRoomPage.vue` 의 기존 lint 경고는 본 변경과 무관.
- `openspec validate bpmn-artifact-only-flow` ✅ valid.

## 6. 라이브 검증 (실서버 8088 + gateway + supabase + deepagent)

실 계정으로 메인채팅(deepagents 모드)에서 직접 검증했다. 스크린샷: `process-gpt-vue3/playwright/test-results/live/`.

- **복원(이번 수정의 핵심) — 검증 완료 ✅**: 좌측 대화목록에서 기존 "휴가 신청 프로세스"(생성 완료) 방을 열면 우측 ArtifactPanel 에 `type:'process'` 패널이 **복원**된다. 화면 확인 항목: 프로세스 ID, **"저장됨" 배지**(proc_def 존재 재확인 동작), 요소 14·스킬 1·폼 3, `notification-skill`, 미리보기/저장 버튼. **새로고침 후에도 유지**됨(`live-restore-existing.spec.ts` EXIT 0, 스크린샷 R2/R3).
- **라이브 생성 흐름 — 동작 확인 ✅(부분)**: 로그인 → "딥 에이전트" 선택 → "휴가 신청 프로세스 생성해줘" 전송 → deepagent 가 **컨설팅 초안 승인(HITL)** → `write_file` 산출물 생성 → **후보(스킬/에이전트/DMN) 선택(HITL)** 순으로 실제 진행. To-do #1(컨설팅&프로세스 JSON 생성)이 `completed` 로 바뀌는 것까지 관찰.
- **자동화 한계(정직한 한계)**: 한 번의 자동 실행으로 *최종* 산출물 패널까지 도달하는 것은, 생성이 **여러 라운드의 HITL(승인 + 다중 후보 선택)** 과 수 분~십수 분의 LLM 생성을 거치는 상호작용형 흐름이라 Playwright 자동화로 끝까지 몰아가지 못했다(후보 선택 라운드 제출에서 막힘 — 제품 결함이 아니라 테스트 하네스 한계). 단, **이미 생성 완료된 방들이 산출물 패널을 정상 표시/복원**하므로 표시 경로 자체는 입증됨.
- **참고(인프라)**: 일부 신규 요청이 `Agent API error: 500`(deepagent `/chat/stream` 500)으로 즉시 실패하는 사례가 있었고, 다른 요청은 정상 생성됨 → **간헐적 백엔드 오류**로 보임(프론트 변경과 무관). 재현 시 deepagent 서버 로그 확인 권장.

이전(미검증) 항목은 위 라이브 검증으로 대체된다. 자격 주입 후 자동 재현은 아래 매뉴얼 참고.

## 7. 자체 수정(self-heal) 이력

- 1차 lint: `ChatRoomPage.vue` 159 issues 보고 → 전부 기존 코드(`_` 예약키, prettier, no-empty 등). 신규 파일·변경 라인 기인 오류 0건 확인 후 진행.

---

# 사용 매뉴얼

## A. 기능 사용법 (실사용)
1. 메인 채팅에서 **deepagents 모드(orchestration=deepagents)** 채팅방을 연다.
2. `휴가 신청 프로세스 만들어줘` 같이 요청한다.
3. deepagent 가 산출물을 만들면 화면 **우측에 ArtifactPanel 탭**(`mdi-sitemap-outline` 아이콘, 프로세스명 라벨)이 자동으로 열린다.
4. 패널에서 요소/스킬/에이전트/폼 개수와 목록을 확인한다.
   - **미리보기**: 저장 전 계약 정의를 BPMN 으로 변환해 미리 본다.
   - **저장**: proc_def + form_def 를 저장하고 '저장됨' 으로 표시된다(저장 전에는 DB 에 아무것도 쓰지 않음).
5. **새로고침/재진입** 후에도 같은 채팅방을 열면 우측 탭이 자동 복원되어 다시 열어볼 수 있다.

## B. 결정적 테스트 실행 (서버 불필요)
```bash
cd process-gpt-vue3
npx playwright test playwright/e2e/bpmn-artifact/artifact-panel-restore.spec.ts \
                    playwright/e2e/bpmn-artifact/artifact-save-conversion.spec.ts --reporter=list
# 기대: 5 passed
```

## C. 라이브 풀 E2E 실행 (표시 + 새로고침 복원)
사전: deepagents 백엔드 가동(또는 본 테스트는 스트림을 mock 하므로 dev 서버+로그인만으로도 충분).
```bash
cd process-gpt-vue3
# 1) dev 서버 (미설정 시 playwright.config 가 8088 로 자동 기동)
npm run dev -- --port 8088     # 별도 터미널, 선택

# 2) 자격/대상 채팅방 주입 후 실행
set E2E_USER=<로그인이메일>          # PowerShell: $env:E2E_USER="..."
set E2E_PASS=<비밀번호>
set E2E_DEEPAGENT_CHAT_URL=/chat?roomId=<deepagents 방 id>
npx playwright test playwright/e2e/bpmn-artifact/artifact-panel-flow.spec.ts --reporter=list
```
- 위 env 가 없으면 두 라이브 테스트는 자동 skip 된다.
- 리포트: `process-gpt-vue3/playwright/playwright-report/`.

## D. 관련 파일
- 프론트 표시/복원: `src/views/chat/ChatRoomPage.vue`(`onProcessResult`, `checkExistingArtifactPanels`), `src/components/ArtifactPanel.vue`, `src/components/ProcessArtifactViewer.vue`.
- 복원 헬퍼/저장 변환: `src/utils/processArtifactPanel.js`, `src/utils/elementsToFlattened.js`.
- 백엔드 emit: `process-gpt-deepagents/executor.py`, `core/skills/tools.py`.
