"""대화창 인계로 넘긴 파일이 받는 쪽 작업 폴더에 닿는다(2026-10-20).

실환경 견적 업무에서 두 가지가 막혔다.
  - 한글 이름 산출물('견적서_한빛상사.docx')이 저장소 키 검사에 걸려 작업 폴더 동기화에서 빠졌다.
  - 에이전트가 인계받은 할 일은 폴러로 실행돼 방 작업 폴더를 쓰지 않았다.
"""
from core.agents.input_builder import resolve_handover_room_id
from core.storage import storage


def test_korean_file_name_becomes_a_valid_storage_key_and_comes_back():
    rel = "견적/견적서_한빛상사_서버3대.docx"
    key = storage.encode_key_path(rel)
    assert storage._SAFE_KEY_SEGMENT.match(key), key
    assert storage.decode_key_path(key) == rel


def test_ascii_names_keep_their_key():
    for rel in ("process-definition.json", "forms/write_estimate.form", "generate_hanbit_quote.js"):
        assert storage.encode_key_path(rel) == rel


def test_storage_path_encodes_only_the_relative_part():
    key = storage._storage_path("localhost", "room-1", "견적서.docx")
    assert key.startswith("skills/localhost/room-1/")
    assert storage._SAFE_KEY_SEGMENT.match(key), f"저장소가 거부할 키: {key}"
    assert storage.decode_key_path(key.rsplit("/", 1)[1]) == "견적서.docx"


def test_hydration_restores_the_original_name(monkeypatch, tmp_path):
    """업로드한 키를 내려받으면 원래 이름으로 작업 폴더에 놓인다."""
    prefix = "skills/localhost/room-1"
    # 업로드가 남긴 키(인코딩된 한글 이름)
    keys = [f"{prefix}/{storage.encode_key_path('견적서.docx')}", f"{prefix}/plain.txt"]

    class Bucket:
        def download(self, key):
            return key.encode()

    class Client:
        class storage:  # noqa: N801
            @staticmethod
            def from_(_):
                return Bucket()

    monkeypatch.setattr(storage, "_require_tenant_id", lambda: "localhost")
    monkeypatch.setattr(storage, "_validate_workspace_path", lambda *a: None)
    monkeypatch.setattr(storage, "get_supabase", lambda: Client())
    monkeypatch.setattr(storage, "_list_all_objects", lambda c, p: keys)
    storage.download_workspace_directory("room-1", tmp_path)
    assert (tmp_path / "견적서.docx").exists()
    assert (tmp_path / "plain.txt").exists()


def test_handover_task_resolves_its_room():
    room = "7742c007-9865-68af-e57b-693f5106197a"
    row = {"adhoc": True, "query": f"[Handover] 홍길동 님이 넘긴 업무\n\n[Instruction]\n견적서 작성\n\n[ChatRoom] {room}"}
    assert resolve_handover_room_id(row) == room


def test_other_tasks_do_not_resolve_a_room():
    assert resolve_handover_room_id({"adhoc": False, "query": "[ChatRoom] 7742c007-9865-68af-e57b-693f5106197a"}) == ""
    assert resolve_handover_room_id({"adhoc": True, "query": "견적서 작성"}) == ""
    # 지시문 중간에 적힌 글은 방 표식이 아니다
    assert resolve_handover_room_id({"adhoc": True, "query": "내용 [ChatRoom] abcdefgh-1234 참고"}) == ""


def test_handover_result_is_readable_text_not_form_json():
    """할 일 폴러의 폼값 JSON 이 방에 그대로 보이지 않는다."""
    import executor

    raw = '{"defaultform": {"free_input": "견적서 파일을 생성했습니다. 경로: 견적서_대한물산.docx"}}'
    assert executor._readable_result(raw) == "견적서 파일을 생성했습니다. 경로: 견적서_대한물산.docx"
    assert executor._readable_result("그냥 글") == "그냥 글"


def test_handover_agent_name_falls_back_to_users(monkeypatch):
    """실행 목록에 이름이 없어도 발신자가 'agent:<id>' 로 보이지 않게 이름을 찾는다."""
    import executor
    from core.storage import db

    class _Res:
        data = [{"username": "문서 생성 전문가"}]

    class _Q:
        def select(self, *a):
            return self

        def eq(self, *a):
            return self

        def limit(self, *a):
            return self

        def execute(self):
            return _Res()

    class _C:
        def table(self, name):
            assert name == "users"
            return _Q()

    monkeypatch.setattr(db, "get_supabase", lambda: _C())
    got = executor._handover_agent({"user_id": "1b76edfd"}, [])
    assert got == {"id": "1b76edfd", "name": "문서 생성 전문가"}
