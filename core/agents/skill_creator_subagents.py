"""core/agents/skill_creator_subagents.py — 규칙 추출 / 테스트 실행 / 결과 평가 서브에이전트 정의.

build_skill_creator_subagents()를 호출해 SubAgent 목록을 얻은 뒤
build_agent(static_subagents=...) 로 전달한다.

포함된 서브에이전트:
  repo-scout      — skill 생성 전 GitHub에 동일 스킬이 있는지 탐색
  rule-extractor  — Supabase storage 문서를 60/40 분할해 규칙 추출
  test-runner     — eval 프롬프트를 스킬 유무에 따라 실행하고 출력 저장
  evaluator       — grader.md 지침으로 assertion 채점 및 grading.json 생성
"""

from __future__ import annotations

from pathlib import Path

from deepagents import SubAgent

from ..skills.tools import (
    WORKSPACE_ROOT,
    extract_document_structure,
    list_github_repos,
)
from ..storage.storage import download_file, upload_artifact, upload_directory

_BASE_DIR = Path(__file__).parent.parent.parent

# skill-creator 스킬 폴더 기본 경로
_DEFAULT_SKILL_CREATOR_PATH = _BASE_DIR / "skills" / "anthropics-skills" / "skill-creator"


# ---------------------------------------------------------------------------
# 레포지토리 탐색 서브에이전트 시스템 프롬프트
# ---------------------------------------------------------------------------

_REPO_SCOUT_SYSTEM = """\
You are a **repository-scout sub-agent** in the skill-creator pipeline.
You are invoked at the START of every skill creation request, before any rule
extraction or skill drafting, to determine whether the requested skill already
exists in GitHub.

## Inputs from task description

  - `skill_request`: Natural-language description of the requested skill.
  - `skill_name_hint`: (optional) Suggested repo/folder name for the skill.

## Steps

### Step 1 — List existing repos

Call `list_github_repos()` (no arguments). It returns a JSON array of objects:
```
[{"name": "...", "description": "...", "html_url": "...", "updated_at": "...", "default_branch": "..."}, ...]
```

### Step 2 — Find matches

Compare `skill_request` and `skill_name_hint` against each repo's `name` and
`description`. Classify the best match as one of:

- **exact**: repo name or description directly matches the requested skill
- **partial**: existing repo is a subset, superset, or close variant of the request
- **none**: no relevant repo found

### Step 3 — Return recommendation

Output a single JSON block (no surrounding prose):

```json
{
  "decision": "CREATE_NEW" | "MODIFY_EXISTING" | "CREATE_BRANCH",
  "skill_name": "<suggested kebab-case repo/folder name>",
  "existing_repo_url": "<GitHub URL or null>",
  "existing_repo_name": "<repo name or null>",
  "suggested_branch": "<branch name or null>",
  "reasoning": "<1-2 sentences explaining the decision>"
}
```

Decision rules:
- **CREATE_NEW** — no existing repo matches the requested skill.
- **MODIFY_EXISTING** — an existing repo covers the same skill and the change is an
  update or improvement to the current main version (description clarification,
  minor rule addition, bug fix).
- **CREATE_BRANCH** — an existing repo covers the same skill but the change is
  significant (major refactor, new version, experimental approach). Suggest a
  branch name like `v2` or `feature/<topic>`.

## Constraints

- Do NOT download, clone, or read any files from GitHub — compare names and
  descriptions only.
- Do NOT create, push, or modify any repository.
- Return ONLY the JSON block as your final answer.
"""


# ---------------------------------------------------------------------------
# 규칙 추출 서브에이전트 시스템 프롬프트
# ---------------------------------------------------------------------------

def _build_rule_extractor_system(ws: str) -> str:
    return f"""\
You are a **rule-extraction sub-agent** in the skill-creator pipeline.
You are invoked when the task description lists Supabase storage paths for
reference documents that should be analysed to build skill knowledge.

Your job is to:
1. Split the provided storage paths into a 60/40 train/holdout set
2. Download only the training files using `download_file`
3. For each training file, call `extract_document_structure` to get the FULL structured
   representation — body text PLUS all annotation elements (review comments, footnotes,
   endnotes, track changes, PDF sticky notes, etc.)
4. Identify (anchor_text, annotation) pairs from the annotation elements
5. Accumulate knowledge into knowledge.md after each file, then discard from context
6. Generalise recurring annotation patterns into explicit, actionable rules
7. Save extracted rules and the holdout split manifest

Your task description will specify:
  - storage_file_paths: list of **Supabase storage paths** in the `files` bucket
  - skill_root: root directory of the skill being created
  - rules_output_path: <skill_root>/references/extracted-rules.md
  - split_manifest_path: <skill_root>/evals/split-manifest.json
  - split_ratio: fraction to use for extraction (default 0.6)

## Environment

  - **`download_file(storage_path, dest_dir) → local_path`**: downloads a single file
    from the `files` bucket into `dest_dir` and returns the absolute local path.
  - **`extract_document_structure(file_path)`** — ALWAYS use this tool to parse documents.
    It returns a structured text block containing:
      - `--- Body Text ---` section
      - `--- Annotation Elements (N found) ---` section listing every
        `(anchor_text, content)` pair with type, author, and date fields.
    Do NOT use `read_file`, `execute`, or shell commands to read document content.
  - **Workspace**: `{ws}` (all file I/O must stay inside this directory)

## Process

### Step 0: Prepare workspace

```python
execute("""
import pathlib
workspace = pathlib.Path("{ws}")
(workspace / 'docs').mkdir(parents=True, exist_ok=True)
(workspace / 'knowledge.md').touch()
""")
```

All subsequent paths use `{ws}/` as the root (referred to as `$WS` below).

### Step 1: Split documents (60 / 40)

Randomly divide the `storage_file_paths` list:
- **Training set (60%)**: downloaded and ingested for pattern extraction
- **Holdout set (40%)**: storage paths reserved as eval inputs — do NOT download these

Edge cases:
- 1 document  → all training; note holdout is empty, synthetic evals needed
- 2 documents → 1 training, 1 holdout
- 3 documents → 2 training, 1 holdout
- Always ensure holdout has ≥ 1 document when total ≥ 2

### Step 2: Download training documents

For each training storage path, call:

```
local_path = download_file("<storage_path>", dest_dir="{ws}/docs")
```

`download_file` places the file inside `{ws}/docs/` and returns the absolute local path.
Do NOT download holdout files.

### Step 3: Parse each training document with `extract_document_structure`

For each downloaded file, call:

```
structured_text = extract_document_structure("<local_path>")
```

**CRITICAL**: You MUST call `extract_document_structure` — never use `read_file`,
`run_shell`, or `execute` to read the document content directly.
This tool handles PDF / DOCX / TXT / MD automatically and returns:
- DOCX: body text + review comments (with exact anchor), footnotes, endnotes, track changes
- PDF: body text + sticky notes and highlighted annotations
- TXT/MD: body text + HTML comments, GFM callouts, Markdown link titles
- Unknown formats: body text only with a parser_notes warning

After calling the tool, **immediately delete the downloaded file**:
```
execute("rm {ws}/docs/<filename>")
```
Do NOT write the structured text to disk — hold it in context only for Step 4.

### Step 4: Sequential ingestion — annotation pairs + body patterns

Process the `structured_text` returned by `extract_document_structure` **while it is
still in context**. After updating `{ws}/knowledge.md`, release the content.

#### 4a. Identify annotation pairs

From the `--- Annotation Elements ---` section of `structured_text`, extract every
`(anchor_text, content)` pair. For each pair, record:
- `type`: review_comment | footnote | endnote | track_deletion | pdf_note | …
- `author` and `date` if present

**Annotation pairs are the PRIMARY knowledge signal** — they capture expert judgment
directly attached to specific text. Do not skip or abbreviate this step.

If `parser_notes` says "no annotation extraction available" OR the Annotation Elements
section shows "none found", fall back to body text patterns only and note this as a
limitation.

#### 4b. Append to `{ws}/knowledge.md`

```markdown
## [filename] (doc N of M)

### Annotation Pairs
| anchor_text | annotation | type | author |
|-------------|------------|------|--------|
| "..." | "..." | review_comment | 홍길동 |
| "..." | "..." | footnote | — |

### Body Patterns
- **Structure**: sections present, ordering, document anatomy
- **Content conventions**: recurring clauses, formulas, data formats
- **Style**: tone, terminology, naming conventions
- **Process logic**: implied workflows, decision trees
- **Exceptions / edge cases**: special handling encountered

### Cross-document Status
- **Confirmed**: <pairs/patterns also seen in prior docs>
- **Candidate**: <pairs/patterns seen for the first time>
- **Contradiction**: <variations from an existing pattern>
```

Continue until all training documents are processed.

### Step 5: Generalise confirmed patterns into rules

Two evidence sources feed rule promotion:

1. **Annotation pairs** (primary): same anchor pattern receiving the same type
   of annotation across ≥ 2 documents → strong signal for a rule.
   Example: multiple docs have "납기 조항" anchors with footnotes explaining
   "영업일/달력일 기준 명시" → Rule: "납기 조항에는 영업일/달력일 기준을 명시한다."

2. **Body patterns** (secondary): structural or stylistic recurrences in body
   text, confirmed across ≥ 2 training docs.

Promote a pattern to a rule when it is **confirmed**. For each rule:

- State it as an actionable directive (imperative mood)
- Explain **why** it matters (derive from annotation content where possible)
- Attach a short example: the anchor_text and its annotation
- Tag evidence: `annotation-driven` | `body-pattern`
- Tag frequency: `universal` (all docs) | `common` (>50%) | `occasional` (<50%)

Group rules into logical categories (Structure, Content, Style, Process, Edge Cases).
Within each category, order from most universal to least.

Patterns that remain **candidate** go into "Tentative observations" — do not promote them.

### Step 6: Save outputs

**rules_output_path** — Markdown:

```markdown
# Extracted Rules
_Derived from N_train training documents (60% of N_total total)_

## [Category]
### Rule: [Actionable statement]
- **Why**: ...
- **Evidence**: annotation-driven | body-pattern
- **Frequency**: universal | common | occasional
- **Example**: anchor: "..." → annotation: "..."

---
## Tentative observations (single-document patterns)
- ...
```

**split_manifest_path** — JSON:

```json
{{
  "split_ratio": 0.6,
  "total_documents": 10,
  "training_documents": ["storage/path/a.docx", ...],
  "holdout_documents": ["storage/path/c.docx", ...],
  "rule_count": 12,
  "categories": ["Structure", "Content", "Style"]
}}
```

### Step 7: Report to main agent

Return a concise summary:
1. Training vs. holdout document counts and storage paths
2. Number of rules extracted, broken down by category
3. Top 3 most universal patterns
4. Exact paths where rules and manifest were saved
5. **For each holdout document**: a suggested eval prompt for `evals/evals.json`

## Rules for this agent
- Extract only patterns with evidence — never invent rules
- All file I/O stays inside `{ws}/`
- **Always** call `extract_document_structure` to read documents — NEVER use `read_file`,
  `run_shell`, `execute`, or any other method to extract document content
- Process training documents **sequentially**, updating knowledge.md after each
- Annotation pairs are the PRIMARY signal — capture every (anchor_text, content) pair
- Prefer general, reusable rules over document-specific ones
- Keep rule statements concise and actionable
- Do not copy large blocks of source text into the rules file
- Holdout documents must NOT be parsed or read during ingestion
"""


# ---------------------------------------------------------------------------
# 테스트 실행 서브에이전트 시스템 프롬프트
# ---------------------------------------------------------------------------

_TEST_RUNNER_SYSTEM = """\
You are a **test-execution sub-agent** in the skill-creator evaluation pipeline.

Your task description will specify:
  - skill_path: path to the skill folder to use (or "none" for a baseline run)
  - skill_root: the root directory of the skill being evaluated
  - prompt: the eval task to execute
  - outputs_dir: where to save all output files
    (always under <skill_root>/iteration-<N>/eval-<name>/<variant>/outputs/)
  - variant: "with_skill" or "without_skill"

## Path conventions

All evaluation artifacts live **inside the skill root directory**:
  - Eval definitions:  <skill_root>/evals/evals.json
  - Run outputs:       <skill_root>/iteration-<N>/eval-<name>/with_skill/outputs/
  - Baseline outputs:  <skill_root>/iteration-<N>/eval-<name>/without_skill/outputs/
  - Timing data:       <skill_root>/iteration-<N>/eval-<name>/<variant>/timing.json

## How to run

1. Read `outputs_dir`, `prompt`, `skill_path`, and `variant` from your task description.
2. Create the outputs directory if it doesn't exist.
3. **If skill_path is provided (with_skill run)**:
   - Read the SKILL.md at `skill_path/SKILL.md`
   - Follow its instructions to complete the task prompt
   - Use any scripts bundled with the skill (in `skill_path/scripts/`)
4. **If skill_path is "none" (baseline run)**:
   - Complete the task prompt using your own capabilities, without any skill
5. Save every created or transformed output file to `outputs_dir`.
6. If you encounter uncertainties or errors, write a brief `user_notes.md`
   in `outputs_dir` — but do not stop working.
7. After saving outputs, write `outputs_dir/../timing.json`:
   ```json
   {"executor_duration_seconds": <float>, "total_duration_seconds": <float>}
   ```
   Use the actual wall-clock time you spent.
8. Upload the outputs directory to Supabase storage:
     upload_directory(task_id, outputs_dir)
   where `task_id` comes from your task description.

## Rules
- Do the work — do not just explain what you would do.
- Save ALL produced files to `outputs_dir`, not to other locations.
- Keep `outputs_dir` clean: only put task outputs there, not temporary files.
- Do not fabricate outputs that the task did not actually produce.
"""


# ---------------------------------------------------------------------------
# 결과 평가 서브에이전트 시스템 프롬프트 (grader.md 내용 포함)
# ---------------------------------------------------------------------------

_EVALUATOR_PREAMBLE = """\
You are a **result-evaluation sub-agent** in the skill-creator eval pipeline.

Your task description will specify:
  - eval_name: name of the eval case
  - skill_root: the root directory of the skill being evaluated
  - outputs_dir: directory containing the skill's output files
    (always under <skill_root>/iteration-<N>/eval-<name>/<variant>/outputs/)
  - transcript_path: path to the execution transcript (may not exist)
  - assertions: JSON array of assertion strings to grade
  - grading_path: where to save the grading.json result
    (always under <skill_root>/iteration-<N>/eval-<name>/<variant>/grading.json)
  - skill_name: kebab-case name of the skill (e.g. "nda-reviewer")
  - iteration_dir: full path to the iteration directory (e.g. <skill_root>/iteration-1)
  - ws: workspace root path (for viewer.html output)

## Path conventions

All evaluation artifacts live **inside the skill root directory**:
  - Grading results: <skill_root>/iteration-<N>/eval-<name>/with_skill/grading.json
  - Baseline grades: <skill_root>/iteration-<N>/eval-<name>/without_skill/grading.json
  - Benchmark:       <skill_root>/iteration-<N>/benchmark.json
  - Feedback:        <skill_root>/iteration-<N>/feedback.json

Read the assertions from your task description (they are JSON-encoded strings).
Then follow the grading instructions below precisely.

---
"""


def _build_evaluator_system(skill_creator_path: Path) -> str:
    """grader.md 내용을 포함한 평가 서브에이전트 시스템 프롬프트를 빌드한다."""
    grader_path = skill_creator_path / "agents" / "grader.md"
    grader_instructions = grader_path.read_text() if grader_path.exists() else ""

    task_suffix = f"""
---

## Your task

1. Parse `assertions` from your task description (JSON array of strings).
2. Grade each assertion against the outputs and transcript.
3. Save `grading.json` to `grading_path` specified in your task description.
4. Be objective, cite specific evidence, apply no partial credit.
5. Upload `grading.json` to Supabase storage:
     upload_artifact(task_id, grading_path)
   where `task_id` comes from your task description.

## Generating the eval viewer (run immediately after grading)

After saving and uploading grading.json, generate a static HTML viewer:

1. Run benchmark aggregation:
   ```
   execute("cd {skill_creator_path} && python -m scripts.aggregate_benchmark <iteration_dir> --skill-name <skill_name>")
   ```

2. Generate the static viewer:
   ```
   execute("python {skill_creator_path}/eval-viewer/generate_review.py "
           "<iteration_dir> "
           "--skill-name <skill_name> "
           "--benchmark <iteration_dir>/benchmark.json "
           "--static <ws>/viewer.html")
   ```

3. Upload the viewer:
   ```
   upload_artifact(task_id, "<ws>/viewer.html")
   ```

4. Include the viewer download URL in your response to the main agent.
"""
    return _EVALUATOR_PREAMBLE + grader_instructions + task_suffix


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------

def build_skill_creator_subagents(
    task_id: str = "",
    skill_creator_path: Path | None = None,
) -> list[SubAgent]:
    """skill-creator 파이프라인 서브에이전트 목록을 반환한다.

    반환값을 build_agent(static_subagents=...) 에 그대로 전달하면 된다.
    FilesystemMiddleware(ls, read_file, write_file, edit_file, glob, grep)와
    execute 도구는 deepagents가 자동으로 주입한다.

    Args:
        task_id:             현재 작업 ID (rule-extractor workspace 경로에 사용).
        skill_creator_path:  skill-creator 스킬 폴더 경로.
                             None이면 anthropics-skills/skill-creator 기본값 사용.
    """
    sc_path = Path(skill_creator_path) if skill_creator_path else _DEFAULT_SKILL_CREATOR_PATH
    ws = str(WORKSPACE_ROOT / (task_id or "tmp"))

    repo_scout: SubAgent = {
        "name": "repo-scout",
        "description": (
            "Triggered at the START of every skill creation request, before rule extraction "
            "or drafting. Calls `list_github_repos` to retrieve all existing skill repos, "
            "then compares the requested skill against them by name and description. "
            "Returns a JSON recommendation: CREATE_NEW (no match found), "
            "MODIFY_EXISTING (update the current main version of a matching repo), or "
            "CREATE_BRANCH (significant change warranting a new branch on a matching repo). "
            "Always invoke this sub-agent first when the user asks to create or build a skill."
        ),
        "system_prompt": _REPO_SCOUT_SYSTEM,
        "tools": [list_github_repos],
    }

    rule_extractor: SubAgent = {
        "name": "rule-extractor",
        "description": (
            "Triggered when the task description includes `storage_file_paths` — a list "
            "of Supabase storage paths for reference documents. "
            "Splits documents 60/40 (train/holdout), downloads only training files via "
            "`download_file`, then calls `extract_document_structure` on each to get the "
            "FULL structured representation — body text PLUS all annotation elements "
            "(review comments, footnotes, endnotes, track changes, PDF sticky notes). "
            "Identifies (anchor_text, content) annotation pairs, accumulates knowledge "
            "into knowledge.md sequentially, and generalises recurring annotation patterns "
            "into explicit rules saved to <skill-root>/references/extracted-rules.md. "
            "The 40% holdout list is saved to <skill-root>/evals/split-manifest.json "
            "and each holdout document becomes one eval test case. "
            "Use this BEFORE writing the skill draft whenever storage_file_paths are provided."
        ),
        "system_prompt": _build_rule_extractor_system(ws),
        "tools": [download_file, extract_document_structure],
    }

    test_runner: SubAgent = {
        "name": "test-runner",
        "description": (
            "Executes an eval prompt with or without a specified skill and saves "
            "all outputs to disk. Use for running skill test cases — both the "
            "with-skill run and the baseline (without-skill) run. "
            "Always spawn the with-skill and baseline runs in the same turn "
            "so they run in parallel."
        ),
        "system_prompt": _TEST_RUNNER_SYSTEM,
        "tools": [upload_directory],
    }

    evaluator: SubAgent = {
        "name": "evaluator",
        "description": (
            "Grades a set of assertions against eval outputs and writes grading.json. "
            "Use after test-runner runs have completed. "
            "Provide the outputs directory, assertions list, and target grading path."
        ),
        "system_prompt": _build_evaluator_system(sc_path),
        "tools": [upload_artifact],
    }

    return [repo_scout, rule_extractor, test_runner, evaluator]
