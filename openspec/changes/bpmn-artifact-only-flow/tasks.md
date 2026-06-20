## 1. 백엔드: 저장·업로드·삭제 제거 (산출물 전용)

- [ ] 1.1 `core/skills/tools.py`: `upload_skill` 도구 제거(스킬은 workspace 산출물 파일로만).
- [ ] 1.2 `complete_process_generation`: workspace 자동 삭제(rmtree) 제거 — 파일 보존. skills 는 `skills/*/SKILL.md` 그대로 산출물에 포함.
- [ ] 1.3 `core/agents/agent.py`: 도구 등록에서 `upload_skill` 제거.

## 2. 백엔드: 스킬 문서 단일화 (모드/업로드 제거)

- [ ] 2.1 `agent.py` 시스템 프롬프트: upload_skill 단계 제거, "스킬=workspace 산출물", workspace 보존, 모드 분기 없음 명시.
- [ ] 2.2 `SKILL.md`·`09`·`04`: upload_skill·CLAUDE_SKILLS 언급 제거, 스킬은 파일 산출물로.
- [ ] 2.3 `00-orientation.md` 제거(또는 비활성), `11-multi-process.md`·`03`의 AskUserQuestion/모드 잔여 제거, 다중/단일·문서/자연어 일관 처리 문구 정리.

## 3. 프론트: ArtifactPanel 에 BPMN 산출물 표시

- [ ] 3.1 `ArtifactPanel.vue`: `type: 'process'` 패널 렌더(프로세스 미리보기 + 스킬/에이전트 목록 + 저장 버튼) 추가 — 기존 BPMN 미리보기/저장 로직 재사용.
- [ ] 3.2 `ChatRoomPage.vue` `onProcessResult`: `pushArtifactPanel({type:'process', ...})` 로 연결(전체 계약 보관).
- [ ] 3.3 저장 버튼 → 기존 `handleSaveGeneratedProcess` 재사용, '저장됨' 상태 표시.
- [x] 3.4 `ChatRoomPage.vue` `checkExistingArtifactPanels`: 채팅방 재진입 시 `pdf2bpmnResult.__contract` 가 있는 메시지에서 `type:'process'` 패널 복원(+ proc_def 존재 시 '저장됨' 표시) — 슬라이드/HWPX 복원과 동일.

## 4. 테스트 & 문서

- [~] 4.1 playwright e2e: 표시 + 새로고침 복원(`artifact-panel-flow.spec.ts`) — 라이브 자격 주입 시 실행(현 환경 skip). 변환·복원 결정 로직은 `artifact-save-conversion`·`artifact-panel-restore` 로 결정적 검증(5 passed).
- [x] 4.2 테스트 실행 후 리포트 작성(`REPORT.md`).
- [x] 4.3 사용 매뉴얼 작성(`REPORT.md` 하단 — 생성 흐름·산출물 확인·저장·복원 방법).

## 5. 검증

- [ ] 5.1 백엔드 컴파일 + 도구 단위 동작 확인.
- [x] 5.2 `openspec validate bpmn-artifact-only-flow` 통과.
