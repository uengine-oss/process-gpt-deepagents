# Extracted Rules: NDA (Non-Disclosure Agreement) Review

Generated from analysis of 3 training documents (6a3b26a7, 39634809, e95b363b) with 29 total annotation instances from Legal Review Team (법무팀 자문).

---

## Rule 1: Secret Information Definition — Balance Breadth and Practicality

**Severity:** HIGH

**Pattern:** The definition of "confidential information" (비밀정보) appears in every NDA and is frequently flagged as either too broad or too narrow.

**Rule:**
- If the definition uses overly broad language such as "구두·서면·전자적 형식 등 모든 형태" (all forms including oral, written, electronic), the NDA effectively covers virtually all information. Replace with: "information that is marked as confidential or that, by the nature of the information and the circumstances of disclosure, reasonably should be understood to be confidential."
- If the definition requires written "Confidential" marking only (서면으로 "Confidential" 표시된 정보에 한한다), it is too narrow for contexts where oral disclosures are common (e.g., SaaS partnerships, technical cooperation). Add a provision that oral disclosures must be confirmed in writing within 30 days to be covered.
- **Checklist for Secret Information Definition:**
  - [ ] Is the definition balanced — not unlimited but not too narrow?
  - [ ] If written marking is required, is there an oral disclosure follow-up mechanism (e.g., 30-day written confirmation)?
  - [ ] Are concrete examples of covered information types listed (e.g., technical specs, business plans, customer lists)?

**Affected clause examples:** `제2조 (비밀정보의 정의)`, `"비밀정보"라 함은`

---

## Rule 2: Four Standard Exceptions — Must Include "Independently Developed"

**Severity:** HIGH

**Pattern:** All 3 training documents had incomplete exception clauses. The standard "four pillars" of NDA exceptions are: (1) already public, (2) already known to receive party, (3) obtained from third party without obligation, (4) independently developed. Every document was missing one or more.

**Rule:**
- The exceptions clause must contain all four standard exceptions:
  1. 공개 당시 이미 공지되었거나 수령자의 귀책 없이 사후에 공지된 정보 (already public)
  2. 수령자가 공개자로부터 받기 이전부터 적법하게 보유하고 있던 정보 (already known)
  3. 제3자로부터 비밀유지 의무 없이 적법하게 입수한 정보 (third party source)
  4. **독자개발(Independently developed) — MUST be added** (developed independently before or after disclosure)
- Without the "independently developed" exception, the receiving party risks having its own independently-created work covered by the NDA.
- **Checklist for Exceptions:**
  - [ ] All four exceptions present (public, already known, third party, independently developed)?
  - [ ] "Independently developed" explicitly included?
  - [ ] Exceptions survive termination of the agreement?

**Affected clause examples:** `제3조 (예외)`, 각 호의 정보`

---

## Rule 3: Confidentiality Period — Market Standard 2–5 Years; Trade Secrets Exempt

**Severity:** HIGH

**Pattern:** All 3 documents had problematic confidentiality periods: 7 years (Doc 1), 5 years + 5 years survival (Doc 2), and indefinite (Doc 3).

**Rule:**
- For general M&A or partnership NDAs, the confidentiality period should be **3 years** from contract termination (market standard is 2–5 years).
- **Indefinite (무기한) confidentiality periods are a red flag**, especially for individual recipients. Courts are likely to interpret them as unreasonably long and reduce them.
- **Trade secrets (영업비밀) should be exempt from the time limit** and protected for as long as they remain trade secrets under applicable law.
- **Checklist for Confidentiality Period:**
  - [ ] Is the period within 2–5 years (3 years preferred for M&A)?
  - [ ] Is "indefinite" used — if so, flag as HIGH severity?
  - [ ] Are trade secrets explicitly exempt from the time limit?
  - [ ] Does the survival period start from contract termination or from the disclosure date?

**Affected clause examples:** `비밀유지 기간`, `7년간`, `5년간 존속`, `기간의 정함 없이(무기한)`

---

## Rule 4: Permitted Related-Party Disclosures — Condition, Not Consent

**Severity:** MEDIUM

**Pattern:** Doc 1 required prior written consent from the disclosing party for all related-party disclosures (employees, advisors). Doc 2 correctly required only that equivalent confidentiality obligations be imposed.

**Rule:**
- Prior written consent (사전 서면 동의) for each disclosure to employees or advisors is **impractical** in real-world M&A due diligence.
- The correct approach: the receiving party may disclose to employees, advisors (accounting, legal, technical), and affiliates **on the condition that** equivalent or higher confidentiality obligations are imposed on them.
- The receiving party should remain responsible for violations by its related parties as if they were its own violations.
- **Checklist for Related-Party Disclosures:**
  - [ ] Is prior written consent required for each disclosure? (Flag as MEDIUM if so — should be condition-based, not consent-based)
  - [ ] Are equivalent confidentiality obligations imposed on recipients?
  - [ ] Is the receiving party held responsible for related-party violations?
  - [ ] Are the categories of permitted recipients clearly defined (employees, advisors, affiliates)?

**Affected clause examples:** `관계자 제공`, `사전 서면 동의를 얻어야`, `동등 이상의 비밀유지 의무`

---

## Rule 5: Liability Cap — Must Not Be Uncapped

**Severity:** HIGH

**Pattern:** Docs 1 and 2 both specified unlimited liability ("직접·간접·결과적 손해 일체", "모든 손해(직·간접 손해 포함)") without any cap.

**Rule:**
- Every NDA must include a **liability cap (책임 상한)**. Uncapped liability for all direct, indirect, and consequential damages creates infinite exposure.
- The cap should be:
  - Limited to **direct damages only** (직접 손해에 한정) — exclude indirect and consequential damages; AND
  - Subject to a **fixed monetary cap** (e.g., transaction value, a defined amount like 1 billion KRW, or 12 months of paid fees).
- For mutual NDAs, the cap should be reciprocal.
- **Checklist for Liability Cap:**
  - [ ] Is there a liability cap? (HIGH severity if absent)
  - [ ] Is the scope limited to direct damages only?
  - [ ] Is the cap amount reasonable and defined?
  - [ ] For mutual NDAs, is the cap reciprocal?

**Affected clause examples:** `책임`, `직접·간접·결과적 손해 일체`, `모든 손해(직·간접 손해 포함)`, `배상`

---

## Rule 6: Non-Solicitation / Non-Compete — Separate from NDA; Reasonable Duration

**Severity:** HIGH

**Pattern:** Doc 1 included a 24-month non-solicitation clause. Doc 3 included a 12-month non-compete clause within the NDA.

**Rule:**
- **Non-solicitation (인력 비권유)** in an NDA should be limited to **12 months** (not 24+). Longer periods may be invalid under Korean unfair trade practices and labor law.
- Non-solicitation should be restricted to **employees learned about through this NDA** (본 계약을 통해 알게 된 임직원), not all employees.
- **Non-compete (경업금지)** should **NOT** be included in an NDA. It should be a separate agreement (e.g., employment contract addendum) with proper consideration (반대급부), reasonable geographic scope, and clear business necessity justification (per Korean Supreme Court precedents).
- **Checklist for Non-Solicitation / Non-Compete:**
  - [ ] If present in NDA, is the period ≤ 12 months?
  - [ ] Is non-solicitation limited to employees known through this agreement?
  - [ ] Is non-compete present? (HIGH severity — should be in a separate agreement)
  - [ ] If non-compete is separate, does it have consideration, geographic limits, and reasonable scope?

**Affected clause examples:** `인력 비권유`, `24개월`, `경업금지`, `동종·유사 사업`

---

## Rule 7: Destruction Certificate — Mandatory Written Confirmation

**Severity:** MEDIUM

**Pattern:** All 3 documents required return/destruction of confidential information but only Doc 2 mentioned written confirmation. Docs 1 and 3 had no destruction certification.

**Rule:**
- Return/destruction obligations must include a **written destruction certificate (파기 확인서)** signed by an officer of the receiving party, delivered within **30 days** of contract termination or the disclosing party's request.
- For individuals (e.g., candidate interviews), this is especially important as information may exist in emails, notes, and various media.
- **Checklist for Destruction:**
  - [ ] Is there a return/destruction obligation?
  - [ ] Is a written destruction certificate required?
  - [ ] Is the timeline specified (e.g., within 5 business days for return, 30 days for certificate)?
  - [ ] Does it cover electronic copies?

**Affected clause examples:** `반환·파기`, `파기 확인서`, `Destruction Certificate`, `파기 사실을 서면으로 확인`

---

## Rule 8: Governing Law and Jurisdiction — Prefer Neutral or Home Jurisdiction

**Severity:** MEDIUM

**Pattern:** Docs 1 and 2 specified Delaware (US) and California (US) law respectively — both the counterparty's home jurisdiction. This creates significant cost and complexity for Korean parties.

**Rule:**
- When the counterparty is foreign, avoid their home jurisdiction as governing law.
- **Preferred tiers:**
  1. **Korean law + Seoul Central District Court** (our home jurisdiction) — first choice
  2. **Singapore International Arbitration Centre (SIAC)** — neutral arbitration as a fallback
  3. Other neutral venues (e.g., HKIAC) — acceptable alternatives
- **Checklist for Governing Law:**
  - [ ] Is the governing law the counterparty's home jurisdiction? (FLAG as MEDIUM — negotiate change)
  - [ ] Is Korean law specified? (Preferred)
  - [ ] Is SIAC or another neutral arbitration specified? (Acceptable)
  - [ ] Is the exclusive jurisdiction court clearly identified?

**Affected clause examples:** `준거법`, `델라웨어주 법`, `캘리포니아주 법`, `관할`, `전속관할`

---

## Rule 9: Notice Provisions — Complete Both Parties, Include Deemed Receipt

**Severity:** LOW

**Pattern:** Doc 1 had no notice provision at all. Doc 2 required email but tied effectiveness to receipt confirmation (unclear). Doc 3 only listed the company's address.

**Rule:**
- Notice provisions must cover **both parties** with specific addresses, emails, and contact persons.
- If email is permitted for notices, include a **deemed receipt provision**: notices sent by email are deemed received after **5 business days** of sending (if no bounce-back).
- Specify the effective date of the agreement (발효 시점).
- **Checklist for Notices:**
  - [ ] Are both parties' contact details specified?
  - [ ] Are permitted notice methods defined (written, email)?
  - [ ] Is a deemed-receipt rule included (e.g., 5 business days for email)?
  - [ ] Is the effective date of the agreement specified?

**Affected clause examples:** `통지`, `서면 또는 이메일로`, `도달된 것으로 본다`, `발효`

---

## Rule 10: Non-Binding / No Obligation to Transact — Essential for M&A NDAs

**Severity:** HIGH

**Pattern:** Doc 1 was missing a clause stating the NDA does not create an obligation to close the transaction. While it stated "no negotiation obligation" (협상 의무 발생시키지 아니한다), it lacked the explicit "no transaction obligation" language.

**Rule:**
- The NDA must explicitly state that it **does not create any obligation to enter into a transaction**, and either party may terminate negotiations at any time.
- This prevents the receiving party from claiming a right to transact or priority negotiation based on the NDA alone.
- **Checklist:**
  - [ ] Is there a clause stating no obligation to close? (HIGH severity if absent)
  - [ ] Does it clarify that either party may terminate negotiations freely?
  - [ ] Is there any clause creating priority negotiation rights or exclusivity? (should be absent or negotiated separately)

**Affected clause examples:** `협상 의무`, `거래 의무`, `No-binding`, `강제하지 않는다`

---

## Rule 11: Residuals Clause — Clarify General Knowledge Rights

**Severity:** MEDIUM

**Pattern:** Doc 2 had no residuals clause, leaving it unclear whether engineers' naturally retained general knowledge could be used in future projects.

**Rule:**
- Include a **residuals clause** that explicitly permits the use of general knowledge and skills retained in the unaided memory of employees (but not specific confidential information or documents).
- Alternative phrasing: "This agreement does not prohibit the use of general knowledge and experience retained in the unaided memory of the receiving party's personnel, provided such use does not involve any specific confidential information, documents, or materials."
- **Checklist:**
  - [ ] Is there a residuals clause?
  - [ ] Does it permit use of general knowledge retained in memory?
  - [ ] Does it explicitly exclude specific confidential information?

**Affected clause examples:** `잔존 지식`, `Residuals`, `일반 지식`, `기억`

---

## Rule 12: Company-to-Individual vs. Company-to-Company — Context Matters

**Severity:** HIGH

**Pattern:** Doc 3 is an NDA between a company and an individual (CTO candidate). Company-to-company NDA templates should not be directly applied to individual recipients without adjustment.

**Rule:**
- When the receiving party is an **individual (natural person)**:
  - Remove or relocate clauses that are inappropriate for individuals (e.g., non-compete, heavy penalties, complex assignment provisions).
  - Non-solicitation and non-compete should be handled in the **employment contract** or a separate agreement, not in the NDA.
  - Penalty amounts (위약금) should be reasonable relative to the individual's income (e.g., 30–50% of annual salary), not fixed large sums.
  - Indefinite confidentiality periods are particularly problematic with individuals.
- **Checklist for Individual Recipients:**
  - [ ] Is the receiving party an individual? If so, flag template-based clauses.
  - [ ] Are non-compete / heavy penalty clauses removed or relocated?
  - [ ] Is the penalty amount reasonable relative to the individual's income?
  - [ ] Is the confidentiality period reasonable (not indefinite)?
  - [ ] Is the relationship between this NDA and any future employment contract clarified?

**Affected clause examples:** `회사 대 개인`, `자연인`, `후보자`, `위약금`, `고용계약`

---

## Rule 13: Mutual vs. Unilateral NDA — Assess Actual Information Flow

**Severity:** MEDIUM

**Pattern:** Doc 2 is labeled "Mutual" (상호) but in many technical cooperation contexts, one party discloses significantly more information than the other.

**Rule:**
- "Mutual" language is not inherently wrong, but if the actual information flow is heavily asymmetric (one side will disclose far more), consider switching to a **unilateral NDA** to avoid the other party making claims about our disclosures.
- Map the expected information flow before deciding.
- **Checklist:**
  - [ ] Is the NDA labeled "mutual"? If so, assess whether information flow is actually symmetric.
  - [ ] If asymmetric, is there a risk of the other party making unfounded claims?
  - [ ] Have the categories of information each party will disclose been mapped?

**Affected clause examples:** `상호`, `Mutual`, `정보 공개의 흐름`

---

## Rule 14: Change of Control — Address M&A Scenarios

**Severity:** MEDIUM

**Pattern:** Doc 2 had an anti-assignment clause but no provision for what happens in a change of control (M&A) scenario.

**Rule:**
- Anti-assignment clauses should address what happens if the counterparty undergoes a **change of control** (e.g., acquisition, merger).
- Options: (a) automatic termination on change of control, or (b) requirement to obtain the disclosing party's consent before any change of control.
- **Checklist:**
  - [ ] Is there an anti-assignment clause?
  - [ ] Does it address change of control scenarios?
  - [ ] Is consent required for change of control?
  - [ ] Is there an automatic termination or opt-out mechanism?

**Affected clause examples:** `양도`, `No-assignment`, `Change of Control`, `M&A`

---

## Rule 15: Personal Information / PII Handling — Applicable to Candidate NDAs

**Severity:** HIGH

**Pattern:** Doc 3 involved a candidate interview, where employee/customer personal information may be shared.

**Rule:**
- When NDAs are used in recruitment/interview contexts, consider whether **personal information (PII)** of employees, customers, or third parties will be shared.
- If so:
  - Include specific PII handling provisions referencing applicable privacy law (e.g., Korean Personal Information Protection Act, PIPA).
  - Alternatively, ensure PII is **de-identified/anonymized** before sharing during the interview process.
- **Checklist:**
  - [ ] Will PII be shared under this NDA?
  - [ ] Is there a PII handling provision or privacy law compliance reference?
  - [ ] Is PII de-identified before sharing?

**Affected clause examples:** `개인정보`, `PII`, `가명정보`, `비식별화`, `개인정보보호법`

---

## Summary Table

| # | Rule | Severity | Occurrences | Key Trigger |
|---|------|----------|-------------|-------------|
| 1 | Secret Info Definition Balance | HIGH | 2/3 | Definition too broad or too narrow |
| 2 | Four Exceptions (incl. Independent Dev.) | HIGH | 3/3 | Missing any of 4 standard exceptions |
| 3 | Confidentiality Period | HIGH | 3/3 | Period outside 2-5yr range, or indefinite |
| 4 | Related-Party Disclosure Conditions | MEDIUM | 2/3 | Prior written consent required |
| 5 | Liability Cap | HIGH | 2/3 | No cap on damages |
| 6 | Non-Solicitation / Non-Compete | HIGH | 2/3 | Present in NDA or excessive duration |
| 7 | Destruction Certificate | MEDIUM | 3/3 | No certification of destruction |
| 8 | Governing Law | MEDIUM | 2/3 | Counterparty's home jurisdiction |
| 9 | Notice Provisions | LOW | 3/3 | Missing or incomplete |
| 10 | Non-Binding Clause | HIGH | 1/3 | No explicit "no obligation to transact" |
| 11 | Residuals Clause | MEDIUM | 1/3 | No clause on residual general knowledge |
| 12 | Individual vs. Company Context | HIGH | 1/3 | Individual recipient with company template |
| 13 | Mutual vs. Unilateral | MEDIUM | 1/3 | Asymmetric information flow |
| 14 | Change of Control | MEDIUM | 1/3 | No provision for M&A scenario |
| 15 | PII Handling | HIGH | 1/3 | Interview/recruitment context |

---

## Source Documents

| Storage Path | File ID | Annotations |
|---|---|---|
| knowledge/localhost/NDA/6a3b26a7-60a9-4dfd-a529-d993f2713816.docx | 6a3b26a7 | 10 annotations |
| knowledge/localhost/NDA/39634809-c495-49ac-9fa9-0ec1677b08cc.docx | 39634809 | 10 annotations |
| knowledge/localhost/NDA/e95b363b-b93f-4698-ad6d-d90d39daa460.docx | e95b363b | 9 annotations |
| knowledge/localhost/NDA/3ca0fe8c-4e2a-414c-b233-f3ac096292b3.docx | 3ca0fe8c | Holdout (not analyzed) |
| knowledge/localhost/NDA/02d818af-43c5-4dc2-b1ba-4b731cae4439.docx | 02d818af | Holdout (not analyzed) |
