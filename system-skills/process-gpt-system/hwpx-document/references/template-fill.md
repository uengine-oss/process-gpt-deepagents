# HWPX 양식 채우기·새로 만들기

## 묶음 구조

```
mimetype                    "application/hwp+zip" — 압축 없이(ZIP_STORED) 첫 항목
version.xml
Contents/content.hpf        묶음 목록(OPF)
Contents/header.xml         글꼴·문단 모양·글자 모양·테두리(id 로 참조됨)
Contents/section0.xml       본문(쪽이 여럿이어도 보통 section0 하나)
Preview/PrvText.txt         미리 보기 글(선택)
Preview/PrvImage.png        미리 보기 그림(선택)
settings.xml
META-INF/container.xml · container.rdf · manifest.xml
BinData/…                   그림
```

## 본문 XML

이름공간: `hp` = `http://www.hancom.co.kr/hwpml/2011/paragraph`, `hs` = `…/2011/section`, `hc` = `…/2011/core`.

```xml
<hp:p id="…" paraPrIDRef="3" styleIDRef="0">
  <hp:run charPrIDRef="7"><hp:t>여기에 글</hp:t></hp:run>
  <hp:linesegarray>…</hp:linesegarray>
</hp:p>
```

- 글은 `<hp:t>` 안에만 있다. 한 문장이 여러 `hp:run` 으로 쪼개져 있을 수 있다(서식이 다른 부분마다) —
  자리 표시 글을 찾을 때는 문단의 `hp:t` 들을 이어 붙여 찾는다.
- 표: `<hp:tbl>` → `<hp:tr>` → `<hp:tc>` → `<hp:subList>` → `<hp:p>`. 칸마다 `<hp:cellAddr colAddr rowAddr>`,
  `<hp:cellSpan>`, `<hp:cellSz width height>`. 행을 늘릴 때는 마지막 데이터 행을 통째로 복사해 `rowAddr` 를 다시 매기고,
  `hp:tbl` 의 `rowCnt` 를 고친다. 합계 행이 있으면 숫자를 다시 계산한다.
- 문단 `id` 는 문서 안에서 겹치지 않게 새로 준다(있는 id 의 최댓값 + 1, 단 2147483648 은 「번호 없음」이라 그 아래에서).
- 글 길이가 크게 바뀐 문단은 `<hp:linesegarray>`(줄 배치 캐시)를 지우면 한글이 열 때 다시 배치한다.
- 서식 id(`paraPrIDRef`·`charPrIDRef`·`borderFillIDRef`)는 header.xml 에 있는 것만 쓴다. 새로 만들지 말고 이웃 문단의 것을 본뜬다.
- 표 행 높이(`hp:cellSz@height`, HWPUNIT)가 고정된 양식이면 글을 늘렸을 때 잘린다 — 줄 수에 맞춰 높이를 키운다
  (어림: 한 줄 ≈ 1125, 기본 여백 ≈ 500).

## 예시(샌드박스 python, 표준 라이브러리만)

```python
import re, shutil, urllib.request, zipfile

def fetch(url, dest):
    url = re.sub(r"//(127\.0\.0\.1|localhost)(?=[:/])", "//host.docker.internal", url)
    with urllib.request.urlopen(url, timeout=30) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)

def rewrite(src, dest, edit):
    """edit(name, bytes) -> bytes. 항목 순서·압축 방식을 원본 그대로 두고 다시 묶는다."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w") as zout:
        for info in zin.infolist():
            data = edit(info.filename, zin.read(info.filename))
            ztype = zipfile.ZIP_STORED if info.filename == "mimetype" else info.compress_type
            zout.writestr(info, data, compress_type=ztype)

def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def fill(name, data, values):
    if name != "Contents/section0.xml":
        return data
    xml = data.decode("utf-8")
    for old, new in values.items():               # 자리 표시 글 → 넣을 글
        xml = xml.replace(f"<hp:t>{esc(old)}</hp:t>", f"<hp:t>{esc(new)}</hp:t>")
    return xml.encode("utf-8")

# fetch(template_url, "/tmp/t.hwpx")
# rewrite("/tmp/t.hwpx", f"{base}/문서.hwpx", lambda n, d: fill(n, d, {"(제목)": "…"}))
```

자리 표시 글이 여러 run 에 쪼개져 있으면 위의 단순 바꾸기로는 안 걸린다 — 그 문단을 ElementTree 로 열어
첫 `hp:t` 에 전체 글을 넣고 나머지 `hp:t` 를 비운다.

## 양식 없이 새로 만들 때

받은 양식이 없으면 직접 OWPML 을 짜기보다, 회사 양식이 등록되어 있는지 먼저 본다(0절의 사실). 정말 없으면
위 구조의 최소 묶음(mimetype · version.xml · Contents/header.xml · Contents/section0.xml · Contents/content.hpf ·
META-INF/container.xml)을 만든다. header.xml 에는 글꼴 1개·글자 모양 1개·문단 모양 1개만 두고 section0.xml 의
모든 문단이 그 id(0)를 참조하게 한다. 이렇게 만든 문서는 서식이 단순하다는 점을 답에 적는다.

## 확인

- `zipfile.ZipFile(path).testzip()` 이 `None`, 첫 항목이 `mimetype` 이고 압축 없음.
- `xml.etree.ElementTree.fromstring(section0)` 이 오류 없이 읽힘.
- 넣은 글이 section0.xml 에 들어 있음.
