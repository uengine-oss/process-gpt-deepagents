# 04 – Skills: skill-creator 서브에이전트로 재사용 스킬 생성 + JSON 반영

> 📁 **경로 주의**: 아래의 `<프로세스폴더>` 는 시스템 프롬프트가 지정한 이 대화의
> 프로세스 폴더 절대경로다([12-deepagents-execution.md](12-deepagents-execution.md)).
> skill-creator 에 넘기는 `output_path` 도 반드시 그 절대경로여야 한다.

**목적**: 2단계에서 사용자가 고른 스킬 후보를 **`skill-creator` 서브에이전트로 정식
스킬(SKILL.md)로 생성**하고, 프로세스 정의 JSON 의 해당 Activity 와 `skills` 목록에 연결한다.

산출물:
- `<프로세스폴더>/skills/<safe-name>/SKILL.md` (선택된 스킬마다 1개 디렉토리)
- `process-definition.json` 업데이트 (`skills[]` 추가 + 관련 `activity.skills` 채움)
- `manifest.json` 의 `skills[]` 매핑([03-elicit-artifacts.md](03-elicit-artifacts.md))

**외부 업로드·DB 저장은 하지 않는다.** 파일이 곧 산출물이고, 저장은 사용자가 프론트에서 한다.

---

## 단계 A. 스킬 브리프 정리 — 프로세스에서 자동 추출

사용자에게 다시 묻지 말고, 근거가 된 Activity 들(`source_activity_ids`)의
`name`/`description`/`instruction` 을 종합해 아래 **스킬 브리프**를 채운다. 이 브리프가
그대로 skill-creator 에 넘길 `context` 가 된다.

| 필드 | 규칙 |
|------|------|
| `safe_name` | 영문 소문자 + 하이픈(kebab-case), 3~6 단어. 한글/공백/특수문자 금지. 예: `leave-balance-check`. **이 값이 스킬 디렉토리 이름**이 된다. |
| `name` | 도메인 의미가 분명한 **한국어 명사구**. "공통지침", "기타", "스킬", "절차" 같은 일반·형식적 단어 금지. 예: "휴가 잔여일수 검증" |
| `description` | frontmatter 용. **무엇을 + 언제 트리거하는지** 구체적으로. |
| `summary` | 3~5 문장 개요. 무엇을, 왜, 어떤 산출물로 만드는지. |
| `when_to_use` | 사용 시점/트리거를 질문·조건 형태로 4~6개. |
| `inputs` | 필요한 입력/사전 조건(서류·레코드·결과코드 등 명사구) 3~5개. |
| `outputs` | 결과물/산출물 2~4개. |
| `procedure` | 단계별 절차 4~7단계. 각 단계 `{ title(한국어 짧은 제목), detail(2~4문장 구체 설명) }`. |
| `examples` | 구체 시나리오 1~2개. 각 `{ scenario, input, output }` 모두 한국어. |
| `notes` | 운영 시 주의/제약/정책 3~5개. |

`safe_name` 이 겹치면 `-2`, `-3` 접미사를 붙여 유일하게 만든다.

> 참고 템플릿: [assets/templates/skill-card.md](../assets/templates/skill-card.md) 의
> 섹션 구성을 활용하면 브리프가 깔끔하게 정리된다.

---

## 단계 B. skill-creator 서브에이전트에 위임 (필수)

**스킬은 직접 손으로 쓰지 않는다.** 선택된 스킬마다
`task(subagent_type="skill-creator")` 를 호출한다. task 설명(description)에 **다음 세
가지를 반드시** 담는다 — 하나라도 빠지면 엉뚱한 경로에 빈약한 파일이 만들어진다:

| 키 | 값 |
|---|---|
| `output_path` | `<프로세스폴더>/skills/<safe_name>/SKILL.md` **절대경로** |
| `skill_name` | 단계 A 의 `name`(한국어) |
| `context` | 단계 A 브리프 전체(summary/when_to_use/inputs/outputs/procedure/examples/notes) + 근거 Activity 의 이름·설명·역할·입출력 |

예시 task 설명:

```
output_path: /workspace/.bpmn/<방>/process-a1b2c3d/skills/leave-balance-check/SKILL.md
skill_name: 휴가 잔여일수 검증
context:
- 목적: 휴가 신청 전 신청자의 잔여 연차를 확인해 신청 가능 여부를 판단한다.
- 근거 활동: apply_leave(휴가 신청서 작성, 역할=신청자), review_leave(휴가 검토, 역할=팀장)
- 입력: 신청자 사번, 신청 기간, 휴가 종류 / 출력: 잔여일수, 신청 가능 여부, 사유
- 절차: 1) 사번으로 연차 원장 조회 2) 신청 기간을 일수로 환산 3) 잔여일수와 비교 …
- 주의: 반차는 0.5일로 계산, 회계연도 경계에 걸치면 연도별로 나눠 차감
```

- 스킬이 여러 개면 **각각 따로** `task()` 를 호출한다(같은 턴에 병렬로 보내도 된다).
- 평가 루프(evals)·벤치마크는 **만들지 않는다.** 프로세스 생성 흐름을 끊기 때문이다.
  사용자가 "이 스킬 제대로 테스트해줘"처럼 명시적으로 요청할 때만 별도로 진행한다.
- 서브에이전트가 실패하거나 파일이 생기지 않았으면(그때만) 같은 브리프로 직접
  `write_file` 해 SKILL.md 를 만든다. 빈 파일·placeholder 로 두지 않는다.

> 스킬 생성이 누락돼도 마무리 단계가 `manifest.json`·`activity.skills` 를 근거로 최소
> 형태의 SKILL.md 를 만들어 채운다. 하지만 그건 **안전망**이지 정상 경로가 아니다 —
> 품질 있는 스킬은 skill-creator 위임에서 나온다.

---

## 단계 C. 프로세스 정의 JSON 반영

`edit_file` 로 아래 필드를 요소 ID 기준 갱신한다
([02-generate-definition.md](02-generate-definition.md)의 변경 규칙 준수):

1. **최상위 `skills` 배열에 추가** (없으면 만든다). 각 항목:
   ```json
   { "id": "<safe_name>", "name": "<한국어 스킬명>", "description": "<요약>" }
   ```
   - `id` 는 단계 A 의 `safe_name` 과 **정확히 일치**(= 스킬 디렉토리 이름).
2. **근거 Activity 의 `skills` 에 그 스킬 id 추가**:
   ```json
   "skills": ["leave-balance-check"]
   ```
   - `source_activity_ids` 에 든 모든 Activity 에 해당 스킬 id 를 넣는다.
3. 스킬이 배정된 Activity 는 자동화 정책상 다음을 함께 설정한다(있으면 유지, 없으면 추가):
   ```json
   "agentMode": "complete",
   "orchestration": "deepagents"
   ```
4. `manifest.json` 의 `skills[]` 에 `{"name": "<safe_name>", "activity_ids": [...]}` 를 기록한다.

> 메인 `elements` 의 Activity 와, (서브프로세스가 있다면) 서브프로세스
> `children.activities` 양쪽 모두에서 id 가 일치하는 항목에 반영한다.

---

## 사용자에게 보여주기

- 만든 스킬마다 한 줄 요약: "**휴가 잔여일수 검증** — 신청 전 잔여 연차 확인 (활동 2개에 연결)"
- 경로·raw JSON 을 들이밀지 말고 자연어로 짧게. 파일 목록은 산출물 패널이 보여준다.

## 다음 단계 연결

2단계에서 에이전트/DMN 도 골랐으면 그 단계로, 아니면 바로 폼으로:
[05-agents.md](05-agents.md) → [06-dmn.md](06-dmn.md) → [07-forms.md](07-forms.md).
