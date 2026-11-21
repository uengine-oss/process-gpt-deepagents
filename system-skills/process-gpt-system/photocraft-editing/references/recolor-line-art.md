# 선 그림의 색을 문서 색조에 맞추기

CAD 렌더·도식처럼 바탕 위에 선이 그려진 그림을 문서 색(예: 남색 계열)에 맞출 때, 밝기로 색을 매기는 변환
(`gradientMap`, `hueSaturation`)을 쓰면 바탕과 밝기가 비슷한 선(빨간 가구 선 등)이 바탕색으로 바뀌어 사라진다.
**바탕색을 기준으로** 칠한다:

```json
photocraft_command_batch {"steps": [
  {"id": "select.magicWand", "params": {"x": 3, "y": 3, "tolerance": 14, "contiguous": false, "antiAlias": false}},
  {"id": "edit.fill", "params": {"contents": "color", "color": "<바탕색, 예 #eef5fc>"}},
  {"id": "select.inverse"},
  {"id": "select.modify.expand", "params": {"radius": 1}},
  {"id": "edit.fill", "params": {"contents": "color", "color": "<선 색, 예 #1f3a6e>"}},
  {"id": "select.deselect"}
]}
```

- `(3,3)` 은 바탕이 보이는 모서리 점이다. 모서리가 바탕이 아니면 바탕이 확실한 다른 점을 고른다.
- 다시 조정하라고 하면 먼저 `select.all` → `edit.fillHistory` 로 원래 그림으로 되돌린 뒤 `select.deselect` 하고 같은 순서로 새 색을 칠한다.
- 칠한 뒤 `photocraft_doc_render_preview` 로 선이 빠짐없이 남았는지 본다.
