# BSC Strategy Interview — Claude Skill

> 경영진과의 Socratic 대화를 통해 균형성과표(BSC) 전략맵을 공동 수립하는 AI 전략 인터뷰어.
> Claude Code CLI와 [process-gpt](https://github.com/uengine-oss/process-gpt) deepagents 채팅
> 양쪽에서 동일하게 동작하도록 설계된 스킬입니다.

## 이 skill은 무엇인가

BSC 프레임워크의 학술적 개념 교육을 완전히 생략하고, 경영자가 이미 쓰는 비즈니스 언어로
재구성된 질문을 통해 그의 머릿속에 파편화되어 있는 전략 구상을 스스로 정렬시킵니다.

- 재무·고객·내부프로세스·학습과 성장 4대 관점을 경영자 지향 용어로 재구성
- 한 번에 하나의 질문만 던지는 Socratic 대화 원칙
- 산업별(B2B SaaS, 리테일/이커머스, 제조) 사전 구축 템플릿으로 1~2분 내 초안 프로토타이핑
- 5단계 Socratic 질문 구조(용어 명확화 → 숨은 가정 추출 → 증거 검증 → 대안 검토 → 인과적 강제 정렬)
- 최종 산출물: Mermaid.js 전략맵 + 선행·후행 KPI 세트

## 설치 (Claude Code)

```bash
git clone https://github.com/uengine-oss/process-gpt-strategy-skill.git ~/.claude/skills/bsc-strategy-interview
```

설치 후 새 세션에서 자연어로 호출:

```
우리 회사 BSC 전략맵 만들고 싶어
```

## 설치 (process-gpt deepagents)

process-gpt 배포에서는 이 레포가 `services/deepagents/system-skills/process-gpt-system/`
아래 git submodule로 포함되어, 테넌트 업로드 없이 모든 테넌트에 자동으로 노출되는
시스템 스킬로 동작합니다. 자세한 내용은 process-gpt 레포의
`core/skills/local_system_skills.py`를 참고하세요.

## 디렉토리 구조

```
process-gpt-strategy-skill/
├── README.md
├── SKILL.md                          # skill manifest
└── references/
    ├── perspectives.md                # BSC 4대 관점의 경영자 지향적 재구성
    ├── socratic-questions.md          # 5단계 Socratic 질문 구조
    ├── industry-templates.md          # 산업별 맞춤형 전략 목표·KPI 템플릿
    ├── validation-checklist.md        # 전략맵 품질 검증 체크리스트
    └── mermaid-template.md            # 최종 Mermaid.js 전략맵 출력 형식
```

## 출처

이 skill의 Socratic 인터뷰 구조와 BSC 관점 재구성 원칙은 사내 process-gpt 프로젝트의
전략 관리 기능(`services/strategy`)과 정렬되도록 설계되었습니다.
