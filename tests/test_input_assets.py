"""Tests for app.executor.input_assets.collect_input_assets."""

from __future__ import annotations

from app.executor.input_assets import collect_input_assets


def test_collect_input_assets_merges_extra_assets_dedup():
    input_data = {"a": "https://example.com/a.pdf"}
    extra = [{"path": "https://example.com/b.xlsx"}, {"url": "https://example.com/c.pdf"}]
    out = collect_input_assets(input_data=input_data, extra_text="", extra_assets=extra)
    paths = [x["path"] for x in out]
    assert paths == [
        "https://example.com/a.pdf",
        "https://example.com/b.xlsx",
        "https://example.com/c.pdf",
    ]
    assert out[0].get("source_origin") == "input_data"
    assert out[1].get("source_origin") == "proc_inst_source"
    # duplicate in extra_assets
    out2 = collect_input_assets(
        input_data=input_data,
        extra_text="",
        extra_assets=[{"path": "https://example.com/a.pdf"}, {"url": "https://example.com/d.pdf"}],
    )
    paths2 = [x["path"] for x in out2]
    assert paths2 == ["https://example.com/a.pdf", "https://example.com/d.pdf"]


def test_collect_input_assets_extra_assets_nested_dict_list():
    sources = [
        {
            "wrapper": [
                {"nested": {"path": "https://x.example/template.pdf"}},
                {"href": "https://y.example/data.csv"},
            ]
        }
    ]
    out = collect_input_assets(input_data={}, extra_text="", extra_assets=sources)
    paths = sorted(x["path"] for x in out)
    assert paths == [
        "https://x.example/template.pdf",
        "https://y.example/data.csv",
    ]


def test_collect_input_assets_extra_assets_none_unchanged():
    input_data = {"u": "https://only.com/f.pdf"}
    out = collect_input_assets(input_data=input_data, extra_text="", extra_assets=None)
    assert len(out) == 1
    assert out[0]["path"] == "https://only.com/f.pdf"


def test_collect_extra_assets_file_name_and_file_path_proc_inst_shape():
    """extras.sources rows often use file_name + file_path (no url/path keys)."""
    extra = [
        {"file_name": "template.pdf", "file_path": "https://storage.example/tenant/t.pdf"},
    ]
    out = collect_input_assets(input_data={}, extra_text="", extra_assets=extra)
    assert len(out) == 1
    assert out[0]["path"] == "https://storage.example/tenant/t.pdf"
    assert out[0]["type"] == "pdf"


def test_collect_extra_assets_full_path_key():
    out = collect_input_assets(
        input_data={},
        extra_text="",
        extra_assets=[{"fullPath": "https://cdn.example/a.pdf"}],
    )
    assert len(out) == 1
    assert out[0]["path"] == "https://cdn.example/a.pdf"
