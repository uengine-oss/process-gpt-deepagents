# BPMN Process Generation — ProcessGPT 시스템 스킬

> 만들고 싶은 업무를 **컨설팅 → 프로세스 정의(JSON) 생성 → 스킬/에이전트/DMN 선택 생성 → 폼 → 참조정보**
> 순서로 함께 만들어가는 skill.
> 사내 **ProcessGPT / pdf2bpmn** 프로젝트의 컨설팅·생성 규칙을 따릅니다.

---

## 이 skill은 무엇인가

BPMN을 몰라도, "휴가 신청 프로세스 만들어줘" 같은 한마디만으로 **실행 가능한 프로세스
정의(JSON)** 를 만들 수 있게 안내합니다. 먼저 흐름 초안을 제안하고, 최소한의 질문으로
함께 다듬은 뒤 ProcessGPT 서비스 규격의 JSON을 생성합니다. 이어서 자동화 요소(재사용
스킬·에이전트·DMN 규칙)를 *고른 것만* 붙이고, 입력 폼과 참조정보까지 연결해 완성합니다.

**핵심 특징**:
- BPMN 초심자도 따라갈 수 있도록 용어를 풀어 설명 (액티비티=사람이 하는 일 한 단계, 게이트웨이=갈림길 등)
- 흐름은 유연하게(컨설팅), 출력 구조는 엄격하게(서비스 규격 JSON 그대로)
- 스킬/에이전트/DMN은 **사용자가 고른 것만** 생성 (HITL)
- **문서 업로드 기반 생성**: 업무 규정·매뉴얼·SOP·양식을 올리면 그 내용에서 흐름을 추출
- **여러 프로세스 한 번에**: 일괄 컨설팅 → 일괄 JSON 생성 → 일괄 후보 선택 → 일괄 마무리
- **선택된 재사용 스킬은 skill-creator 서브에이전트로 정식 SKILL.md 생성**

---

## 실행 환경

이 스킬은 **ProcessGPT 채팅(deepagents)** 을 기본 실행 환경으로 삼습니다.

| 용도 | 도구 |
|---|---|
| 파일 읽기 / 쓰기 / 부분수정 | `read_file` / `write_file` / `edit_file` |
| 사용자에게 묻기(HITL) | `request_human_input` |
| 진행상황 | `write_todos` |
| 업로드 문서 읽기 | `search_documents` (memento RAG) |
| 재사용 스킬 생성 | `task(subagent_type="skill-creator")` |

Claude Code CLI 에서 쓸 때는 도구명만 치환합니다(`read_file`→`Read`, `write_file`→`Write`,
`edit_file`→`Edit`, `request_human_input`→`AskUserQuestion`, `write_todos`→`TodoWrite`,
산출물 경로→`.bpmn/`). 자세한 내용은 [SKILL.md](SKILL.md) 참조.

### 디렉토리 구조

```
bpmn-process-generation-skill/
├── README.md                        # 이 문서
├── SKILL.md                         # skill manifest — 5단계 절차
├── references/                      # 단계별 디테일 가이드
│   ├── 00-orientation.md            # 진입 판별
│   ├── 01-consulting.md             # 컨설팅 규칙
│   ├── 02-generate-definition.md    # 정의 JSON 생성 규칙(엄격)
│   ├── 03-elicit-artifacts.md       # 후보 도출 + HITL 선택 형식 + manifest.json
│   ├── 04-skills.md                 # skill-creator 위임
│   ├── 05-agents.md                 # 에이전트 파일 규격
│   ├── 06-dmn.md                    # DMN 의사결정/규칙
│   ├── 07-forms.md                  # 폼 컴포넌트 규격
│   ├── 08-reference-info.md         # inputData / conditionData
│   ├── 09-service-execution.md      # 실행 모델 + 자체 점검 체크리스트
│   ├── 10-document-intake.md        # 문서 업로드 → 흐름 추출
│   ├── 11-multi-process.md          # 여러 프로세스 일괄 절차
│   └── 12-deepagents-execution.md   # 경로·파일명·도구 제한·자동 마무리
├── assets/templates/                # 스키마·폼 컴포넌트·스킬 카드 템플릿
└── scripts/                         # 정의 변환(flatten) · 정적 검증 보조 (DB 접근 없음)
```

---

## 빠른 시작

채팅에서 자연어로 트리거 (자동 인식):

```
비품 구매 결재 프로세스 만들어줘
```
```
신규 입사자 온보딩 흐름을 자동화하고 싶어
```

호출하면:
1. **컨설팅** — 흐름 초안을 제안하고 승인/반려를 묻습니다 (시스템·도구·소요시간은 묻지 않습니다)
2. 승인하면 **프로세스 정의 JSON** 생성
3. **스킬/에이전트/DMN 후보**를 보여주고 무엇을 만들지 선택 (안 골라도 됨)
4. **폼**과 **참조정보** 연결 → 자체 점검
5. 시스템이 검증·통합해 산출물 패널로 전달 → 사용자가 확인 후 **저장** 버튼

**사용자에게 멈춰 묻는 것은 1·3 두 번뿐**입니다. 나머지는 자동으로 진행됩니다.

---

## 산출물 구조

산출물은 대화별 작업 디렉터리(`<base>/<프로세스폴더>/`)에 파일로 만들어집니다:

```
<프로세스폴더>/
├── process-definition.json      # 메인 산출물. 1단계 생성, 이후 단계에서 계속 업데이트
├── manifest.json                # 선택된 스킬/에이전트/DMN ↔ activity 매핑
├── skills/<safe-name>/SKILL.md  # skill-creator 산출
├── agents/<agent_id>.json       # 에이전트 1명 = 파일 1개
└── forms/<activity_id>.form     # ProcessGPT 폼
```

**여러 프로세스일 때**는 프로세스 폴더가 `process-xxxxxxx`, `process-xxxxxxx-2`, … 로
늘어납니다(구조는 동일).

---

## 저장 정책 (중요)

- 이 스킬은 **DB 에 쓰지 않습니다.** 산출물은 파일까지입니다.
- 턴이 끝나면 시스템이 정의를 검증·보정하고 폼/스킬/에이전트/DMN 을 통합해 프론트로 전달합니다.
- 프론트가 임시저장(draft) 후 실행엔진 `/validate-and-improve` 로 검증합니다.
- **최종 저장은 사용자가 화면에서 '저장' 버튼**을 눌러 수행합니다.

---

## 팁과 주의사항

### 잘 사용하는 방법
- 컨설팅 초안을 꼼꼼히 보고 승인/반려하세요 — 이 한 번이 결과 품질을 좌우합니다.
- "그냥 바로 만들어줘" 라고 하면 컨설팅을 짧게 마치고 생성합니다. 단 정보가 너무 부족하면 핵심 질문 1~2개는 받습니다.
- 산출물을 직접 편집해도 OK. 같은 방에서 "이어서 하자" 하면 남은 단계부터 재개합니다.

### 피해야 할 패턴
- 컨설팅 없이 바로 JSON부터 만들지 않기.
- 스킬/에이전트/DMN을 사용자에게 묻지 않고 다 만들지 않기 (HITL 필수).
- 존재하지 않는 도구(`validate_process_definition` 등) 호출하지 않기.
- placeholder만 남기지 않기 — 실제 내용으로 채우기.

---

## 출처

이 skill의 컨설팅·프로세스 정의·스킬/DMN/폼/참조정보 생성 규칙은 사내 **ProcessGPT /
pdf2bpmn** 프로젝트의 정의를 기반으로 합니다. 흐름·진행 방식은
[ddd-crew/ddd-starter-modelling-process](https://github.com/ddd-crew/ddd-starter-modelling-process)
스타일과 GitHub Spec Kit 의 단계형 사용법을 참고했습니다.

각 단계의 디테일은 [references/](references/) 에, 산출물 템플릿은 [assets/templates/](assets/templates/) 에 있습니다.
