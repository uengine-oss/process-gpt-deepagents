# 08 – Reference Info: 참조정보(inputData / conditionData) 연결

**목적**: 폼이 만들어진 뒤, 각 단계가 **이전 단계의 어떤 입력값을 참조**할지 연결한다. 두 가지를 채운다:
- Activity 의 `inputData`: 이 태스크를 수행할 때 참고할 **이전 태스크 폼의 필드**.
- ExclusiveGateway 의 `conditionData`: 그 분기 조건을 평가할 때 참조할 **이전 태스크 폼의 필드**.

이 단계가 끝나면 프로세스 정의가 완성된다. **이 단계는 5단계(폼) 직후 자동으로 이어 실행되며, 사용자에게 따로 묻지 않는다.** 참조 연결을 마친 뒤 아래 "완료 안내" 로 4·5·6단계 결과를 **한 번에** 요약하고 수정 가능 안내를 한다.

> 이 규칙은 pdf2bpmn 의 `_expand_process_after_forms`(inputData) + conditionData 선택 로직을 옮긴 것입니다.

산출물: `process-definition.json` 최종 업데이트 (`activity.inputData`, `gateway.conditionData`).

---

## 참조 형식

참조값은 항상 **`<form_id>.<field_name>`** 형식이다. 작성 시점의 `form_id` 는 **그 폼이 속한
`activity_id`** 다([07-forms.md](07-forms.md) 의 form_id 규칙). 예: 활동 `apply_leave` 의 폼에
`start_date` 필드가 있으면 참조값은 `apply_leave.start_date`.

저장용 최종 form id(`<프로세스uuid>_<activity_id>_form`)는 마무리 단계가 자동으로 치환하므로
**직접 uuid 를 붙이지 마라.**

---

## 후보는 "선행 태스크"의 폼 필드만

핵심 제약: **반드시 그 노드보다 앞선(predecessor) Activity 의 폼 필드만** 참조할 수 있다. 미래/뒤에 오는 폼이나 존재하지 않는 필드는 참조 금지.

1. Sequence 를 따라 각 노드의 **선행 Activity 집합**을 구한다.
2. 선행 Activity 들의 폼에서 필드(`<form_id>.<field_name>`)를 후보로 모은다.
3. 그 후보 중에서만 고른다.

---

## inputData (각 Activity)

각 UserActivity 에 대해:
- 후보(선행 폼 필드) 중 **이 태스크 수행에 참고하면 좋은 것**만 고른다. 불필요한 참조는 넣지 않는다.
- 적절한 게 분명치 않으면 **선행 후보 전체**를 넣는 폴백도 허용(과하지 않게 상한 내에서).
- 첫 Activity(선행 없음)는 `inputData: []`.

```json
"inputData": ["apply_leave.start_date", "apply_leave.reason"]
```

---

## conditionData (각 ExclusiveGateway)

각 ExclusiveGateway 에 대해:
- **필수 실행 계약**: 바로 전 UserActivity 폼에 분기 판단 전용 `select-field`가 있어야 한다. 선택지의 실제 `value`는 각 outgoing Sequence의 `condition` 문자열과 정확히 같아야 한다(예: `승인`, `반려`). 단순한 요청 메모나 검토 의견 필드는 판단값으로 간주하지 않는다.
- `conditionData`는 위 결정 필드 하나를 우선 참조한다. 값은 반드시 문자열 배열이며 객체(`{"field": ...}`)를 넣지 않는다.
- 그 게이트웨이의 **선행 Activity 들의 폼 필드** 중, 분기 조건을 평가할 때 참조해야 하는 필드만 고른다.
- 후보가 분명치 않으면 폴백: **가장 가까운 선행 Activity 의 모든 폼 필드**를 conditionData 로.
- ParallelGateway/InclusiveGateway 는 조건 평가가 없으므로 `conditionData: []` (비워둔다).

```json
"conditionData": ["leave_approval_form.decision"]
```

폼과 시퀀스 예시:

```html
<select-field name='decision' alias='승인 여부'>
  <option value='승인'>승인</option>
  <option value='반려'>반려</option>
</select-field>
```

```json
{"elementType":"Sequence","source":"approval_gateway","target":"execute","condition":"승인"}
{"elementType":"Sequence","source":"approval_gateway","target":"rework","condition":"반려"}
```

---

## 프로세스 정의 JSON 반영

`edit_file` 로 아래 참조 필드를 요소 ID 기준 갱신:
1. 각 Activity 의 `inputData` 를 선행 후보로 한정해 채운다(중복 제거, 상한 적용).
2. 각 ExclusiveGateway 의 `conditionData` 를 채운다.
3. 메인 `elements` 와 (있으면) 서브프로세스 `children` 양쪽 모두 동기화한다.

> 제약 위반 방지: 채운 모든 참조가 "선행 폼 필드" 후보 집합에 실제로 있는지 확인한다. 없으면 제거한다.

---

## 다음 단계 (자동)

참조정보까지 연결하면 프로세스 정의가 완성된다. **여기서 사용자에게 다시 묻지 말고**
곧바로 자체 점검으로 넘어간다: `process-definition.json` 을 `read_file` 로 다시 읽어
[09-service-execution.md](09-service-execution.md) 의 체크리스트를 대조하고, 결함이 있으면
`edit_file`/`write_file` 로 보정한다.

점검이 끝나면 **더 이상 도구를 호출하지 말고 턴을 끝낸다.** 시스템이 검증·통합해 산출물
패널로 전달하며, 채팅엔 한 줄만 남긴다:

> "프로세스를 생성했어요. 확인 후 저장 버튼을 눌러주세요."

중간 산출물 JSON 을 채팅에 덤프하지 않는다.
