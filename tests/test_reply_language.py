"""영어로 묻는 사용자에게 고정 안내·후보 선택이 영어로도 맞게 도는지."""
import json

from core.chat import ui_text
from core.skills import tools


def test_reply_lang_follows_request_text():
    assert ui_text.reply_lang("Create a bulk order quote process from the selected policy") == "en"
    assert ui_text.reply_lang("선택한 규정대로 견적 프로세스를 만들어 줘") == "ko"
    assert ui_text.reply_lang("BPMN 프로세스 만들어 줘") == "ko"  # 한글이 섞이면 한국어
    assert ui_text.reply_lang("") == "ko"
    assert ui_text.reply_lang("OK") == "ko"  # 영문자 3자 미만은 판단하지 않는다


def test_fixed_texts_and_todos_in_both_languages():
    assert ui_text.t("process_created", "en") == "I've created the process. Review it, then press Save."
    assert ui_text.t("process_created", "ko").startswith("프로세스를 생성했어요")
    assert [x["content"][:2] for x in ui_text.done_todos("en")] == ["1.", "2.", "3.", "4.", "5."]
    assert all(x["status"] == "completed" for x in ui_text.done_todos("ko"))
    assert ui_text.asks_to_save("… Review it, then press Save.")
    assert ui_text.asks_to_save("확인 후 저장 버튼을 눌러주세요")


def test_capture_selection_accepts_english_categories(tmp_path, monkeypatch):
    room = tmp_path / "room"
    (room / "process-1").mkdir(parents=True)
    monkeypatch.setattr(tools, "_bpmn_work_dir", lambda: room)
    monkeypatch.setattr(tools, "process_work_dirs", lambda r, c: [room / "process-1"])
    tools._capture_selection_to_manifest("[Agent] Stock check agent, [Agent] Quote calculation agent, [Skill] Quote pricing")
    man = json.loads((room / "process-1" / "manifest.json").read_text(encoding="utf-8"))
    assert [a["name"] for a in man["agents"]] == ["Stock check agent", "Quote calculation agent"]
    assert [s["name"] for s in man["skills"]] == ["Quote pricing"]


def test_capture_selection_korean_unchanged(tmp_path, monkeypatch):
    room = tmp_path / "room"
    (room / "process-1").mkdir(parents=True)
    monkeypatch.setattr(tools, "_bpmn_work_dir", lambda: room)
    monkeypatch.setattr(tools, "process_work_dirs", lambda r, c: [room / "process-1"])
    tools._capture_selection_to_manifest("[에이전트] 재고 확인 에이전트, [에이전트] 견적 산출 에이전트")
    man = json.loads((room / "process-1" / "manifest.json").read_text(encoding="utf-8"))
    assert [a["name"] for a in man["agents"]] == ["재고 확인 에이전트", "견적 산출 에이전트"]


def test_reply_lang_ignores_system_blocks():
    msg = ("Create a bulk order quote process.\n[ExecutionPolicy]\n- ask_user는 마지막 수단입니다.\n"
           "\n[InputData]\n{\"files\": []}")
    assert ui_text.reply_lang(msg) == "en"
    assert ui_text.reply_lang("견적 프로세스를 만들어 줘\n[ExecutionPolicy]\n- policy") == "ko"


def test_english_request_gets_reply_language_rule():
    from core.chat.ui_text import t
    assert "Reply in English" in t("reply_language_rule", "en")
    assert t("reply_language_rule", "ko") == ""
