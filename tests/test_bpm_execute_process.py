"""딥 에이전트 내장 execute_process — 첫 단계에서만, 한 번만, 엔진이 읽는 역할 배정으로."""

import json
import types

import pytest

from core.bpm import tools as bpm

DEFINITION = {
    "roles": [
        {"name": "사용자", "default": "", "endpoint": ""},
        {"name": "검토자", "default": ["reviewer-uid"], "endpoint": ["reviewer-uid"]},
    ],
    "activities": [
        {"id": "review", "name": "검토", "tool": "formHandler:p_review_form"},
        {"id": "draft", "name": "초안 작성", "role": "사용자", "tool": "formHandler:p_draft_form"},
    ],
    "gateways": [{"id": "start", "type": "startEvent"}],
    "sequences": [{"source": "start", "target": "draft"}, {"source": "draft", "target": "review"}],
}


class _Query:
    def __init__(self, table, store):
        self.table, self.store = table, store

    def __getattr__(self, _name):
        # select / eq / gte / order / limit 은 모두 자기 자신을 돌려준다.
        return lambda *a, **k: self

    def execute(self):
        return types.SimpleNamespace(data=self.store.get(self.table, []))


class _Supabase:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _Query(name, self.store)


@pytest.fixture
def env(monkeypatch):
    store = {"proc_def": [{"id": "p", "name": "보고서", "type": "bpmn", "definition": DEFINITION}], "todolist": []}
    monkeypatch.setattr(bpm, "get_supabase", lambda: _Supabase(store))
    bpm.set_tenant_id("tenant-a")
    bpm.set_user_context({"user_id": "me", "user_email": "me@example.com", "user_name": "나", "user_jwt": "jwt"})
    posted = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            posted.append({"url": url, "json": json, "headers": headers})
            return types.SimpleNamespace(status_code=200, content=b"{}", json=lambda: {}, text="{}")

    import httpx

    monkeypatch.setattr(httpx, "Client", _Client)
    return store, posted


def _call(**kw):
    args = {"process_definition_id": "p", "activity_id": "draft", "form_key": "p_draft_form", "form_values": {"text": "안건"}}
    args.update(kw)
    return json.loads(bpm.execute_process.invoke(args))


def test_later_activity_is_rejected(env):
    _store, posted = env
    out = _call(activity_id="review", form_key="p_review_form")
    assert out["start_activity_id"] == "draft"
    assert posted == []


def test_starts_once_with_engine_shaped_roles(env):
    _store, posted = env
    out = _call(form_key="formHandler:p_draft_form", role_mappings=[{"role": "사용자", "assignees": ["me"]}])
    assert out["process_instance_id"].startswith("p.")
    assert len(posted) == 1
    body = posted[0]["json"]["input"]
    assert posted[0]["url"].endswith("/complete")
    assert posted[0]["headers"]["X-Forwarded-Host"] == "tenant-a"
    # 접두어를 뗀 폼 키, 엔진이 읽는 역할 배정(빠진 역할은 정의 기본값)
    assert body["form_values"] == {"p_draft_form": {"text": "안건"}}
    assert [(r["name"], r["endpoint"]) for r in body["role_mappings"]] == [("사용자", "me"), ("검토자", "reviewer-uid")]


def test_same_values_return_the_existing_instance(env):
    store, posted = env
    store["todolist"] = [{"proc_inst_id": "p.existing", "output": {"p_draft_form": {"text": "안건"}}}]
    out = _call()
    assert out == {**out, "process_instance_id": "p.existing", "already_started": True}
    assert posted == []


def test_detail_returns_start_activities(env):
    out = json.loads(bpm.get_process_detail.invoke({"process_id": "p"}))
    assert out["start_activities"] == [
        {"activity_id": "draft", "name": "초안 작성", "role": "사용자", "form_key": "p_draft_form"}
    ]
