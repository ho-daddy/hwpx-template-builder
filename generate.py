"""라벨 붙은 템플릿 + {라벨: 새값} 딕셔너리 → 실제 hwpx 파일 생성.

핵심 설계: 템플릿은 "원본과 동일한 형식(xml 포함 패키지)"으로 저장한다.
build_template_package()가 원본 hwpx 패키지에서 각 채울 자리(<hp:t>)를 라벨
placeholder(__HWPX_TMPL_라벨__)로 바꾼 "템플릿화된 패키지"를 만든다. 원본의
서식/섹션/표 병합/이미지 등 나머지 구조는 그대로 남는다.

⚠️ 생성(generate_hwpx)은 이 "placeholder가 박힌 템플릿 패키지"를 직접 편집하지
않고, 반드시 **원본 패키지(original_hwpx)** 를 기준으로 돌린다. 그 이유:
placeholder를 박는 순간 문단 텍스트가 짧아지면서 그 문단의 <hp:lineseg> 줄바꿈
캐시가 영구히 잘려나가고, 예컨대 "원래 4줄짜리 문단" 문단을 placeholder(1줄)로
만드는 과정에서 뒤쪽 lineseg가 삭제된다. 그 손상된 파일을 다시 값을 넣어 편집하면
같은(원본과 동일한) 내용을 넣어도 잘려나간 줄바꿈 캐시를 되살릴 수 없어, 표 셀
배치가 밀리거나(셀 내용이 다른 셀로 겹쳐 위치) 텍스트가 줄바꿈 없이 한 줄에
겹쳐 랜더링된다. 그래서 generate는 원본을 그대로 두고 그 위에서 slot을 찾아
값으로 바꾼다. 원본에 이미 값과 같은 텍스트가 있으면(동일 내용 재생성) 아예 그
문단을 건드리지 않아 카피가 원본과 완전히 동일해진다.
"""
from __future__ import annotations

import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

from raw_zip_patch import read_raw_entries, build_zip, trim_lineseg

_T_SPAN_RE = re.compile(r"<hp:t(?:\s+[^>]*?)?/>|<hp:t(?:\s+[^>]*?)?>.*?</hp:t>", re.DOTALL)
_TAB_RE = re.compile(r"<hp:tab\b[^>]*/>")
_HP_NS = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'

# placeholder 마커 — build 전용. 실제 값들은 __라벨__ 형태로 XML 텍스트에 박는다.
_PH = "__HWPX_TMPL_{}__"


class GenerateError(ValueError):
    pass


def _t_text_from_tag(tag_xml: str) -> str:
    """<hp:t>...</hp:t> 조각의 실제 렌더링 텍스트를 복원한다(tab 포함).
    인라인 컨트롤('<hp:tab/>')을 낀 태그라도 자식 tail까지 이어붙여 그 자리의
    현재 텍스트가 정확히 무엇인지 판독할 때 쓴다. app.py의 t_element_text와
    동일한 규칙."""
    if "/>" in tag_xml[:tag_xml.index(">") + 1] and tag_xml.rstrip().endswith("/>"):
        return ""
    wrapped = f"<root {_HP_NS}>{tag_xml}</root>"
    try:
        t_el = ET.fromstring(wrapped)[0]
    except ET.ParseError:
        return ""
    parts = [t_el.text or ""]
    for child in t_el:
        if child.tag.endswith("}tab"):
            parts.append("\t")
        parts.append(child.tail or "")
    return "".join(parts)


def _rendered_len_of_prefix(prefix: str) -> int:
    """has_tab 처리에서 '라벨+<hp:tab/>' 같은 프리픽스의 렌더링 글자수를 센다
    (tab=1글자, 나머지는 태그 제거 후 글자수). 새값 앞부분이 보존되므로 그대로
    유지되는 글자수를 정확히 세어 lineseg 계산에 쓴다."""
    if not prefix:
        return 0
    text_only = re.sub(r"<[^>]+>", "", re.sub(r"<hp:tab\b[^>]*/?>", "", prefix))
    return len(text_only) + prefix.count("<hp:tab")


def _apply_replacement(raw: str, t_occurrence: int, new_value: str, has_tab: bool) -> tuple[str, int, int]:
    """raw 안에서 t_occurrence번째(0-based) <hp:t> 태그의 내용을 새 값으로 교체한다.
    has_tab이면 마지막 <hp:tab/> 뒤 내용만 교체하고 앞(라벨+tab)은 원본 그대로 보존.
    반환: (새 raw, 교체된 <hp:t> 태그의 끝 위치, 새 렌더링 글자수) — 뒤 둘은
    trim_lineseg 인자."""
    spans = list(_T_SPAN_RE.finditer(raw))
    if t_occurrence >= len(spans):
        raise GenerateError(
            f"<hp:t> occurrence {t_occurrence}을(를) 찾을 수 없습니다 (문서에 {len(spans)}개뿐)"
        )
    m = spans[t_occurrence]
    old_tag = m.group()
    escaped_value = escape(new_value)

    tab_matches = list(_TAB_RE.finditer(old_tag)) if (has_tab and not old_tag.endswith("/>")) else []
    if tab_matches:
        prefix_end = tab_matches[-1].end()
        prefix = old_tag[:prefix_end]
        new_tag = prefix + escaped_value + "</hp:t>"
        new_len = _rendered_len_of_prefix(prefix) + len(new_value)
    else:
        new_tag = f"<hp:t>{escaped_value}</hp:t>"
        new_len = len(new_value)

    new_raw = raw[:m.start()] + new_tag + raw[m.end():]
    new_end = m.start() + len(new_tag)
    return new_raw, new_end, new_len


def _iter_slots(structure):
    """template.structure를 순회하며 치환 가능한 자리(문단 또는 표 셀)를 전부 낸다."""
    for block in structure:
        if block.get("type") == "paragraph":
            yield block
        elif block.get("type") == "table":
            for row in block.get("cells", []):
                for cell in row:
                    yield cell


def _read_section_texts(path: Path, match_regex=None):
    """zip에서 section*.xml을 {이름: raw text}로 읽는다."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        pat = match_regex or re.compile(r"[Cc]ontents/section\d+\.xml$")
        return {
            n: zf.read(n).decode("utf-8") for n in names if pat.match(n)
        }


def build_template_package(original_bytes, template):
    """원본 패키지에서 각 라벨 자리의 <hp:t> 텍스트를 __라벨__ placeholder로 바꿔
    "템플릿화된 패키지"를 만든다 (저장용 다운로드 관찰용 산출물).
    tOccurrence가 None(위치 유일 특정 불가)인 자리 또는 라벨이 없는 자리는 건너뛴다."""
    import tempfile as tf
    with tf.TemporaryDirectory() as tmpdir:
        src = Path(tmpdir) / "orig.hwpx"
        src.write_bytes(original_bytes)
        sect = _read_section_texts(src)

        by_sec = {}
        for slot in _iter_slots(template.get("structure", [])):
            lab = slot.get("label")
            if not lab or slot.get("tOccurrence") is None or not slot.get("sectionFile"):
                continue
            by_sec.setdefault(slot["sectionFile"], []).append(slot)

        for sf, slots in by_sec.items():
            raw = sect.get(sf)
            if raw is None:
                continue
            for s in sorted(slots, key=lambda x: x["tOccurrence"], reverse=True):
                ph = _PH.format(s["label"])
                raw, t_end, new_len = _apply_replacement(
                    raw, s["tOccurrence"], ph, bool(s.get("hasTab"))
                )
                raw, _ = trim_lineseg(raw, t_end, new_len)
            sect[sf] = raw
        return _write_package(src, sect, Path(tmpdir) / "template.hwpx")


def generate_hwpx(source_bytes, template, values):
    """원본/패키지(source_bytes) 안의 각 라벨 자리를 새 값으로 치환한 새 hwpx를 만든다.

    반드시 원본(original_hwpx)을 source_bytes로 넘긴다 — placeholder가 박힌 손상
    패키지가 아니라. 각 slot은 template 구조의 tOccurrence(원본 안에서 유일한
    <hp:t> 위치)로 지목된다.

    이미 텍스트가 new_value와 같으면(동일 내용 재생성 등) 아예 건드리지 않아 그
    문단/셀이 원본과 완전히 동일해진다(밀림도, 겹침도 없음). 값이 달라서 길이가
    줄어들기만 하면 그 문단의 lineseg만 정리한다.
    """
    import tempfile as tf
    with tf.TemporaryDirectory() as tmpdir:
        src = Path(tmpdir) / "source.hwpx"
        src.write_bytes(source_bytes)
        sect = _read_section_texts(src)

        by_sec = {}
        for slot in _iter_slots(template.get("structure", [])):
            lab = slot.get("label")
            if not lab or slot.get("tOccurrence") is None or not slot.get("sectionFile"):
                continue
            by_sec.setdefault(slot["sectionFile"], []).append(slot)

        applied = []
        for sf, slots in by_sec.items():
            raw = sect.get(sf)
            if raw is None:
                continue
            for s in sorted(slots, key=lambda x: x["tOccurrence"], reverse=True):
                new_value = values.get(s.get("label"), s.get("text") or "")
                # --- 동일 내용이면 생략 (원본 보존) ---
                try:
                    spans = list(_T_SPAN_RE.finditer(raw))
                    if s["tOccurrence"] < len(spans):
                        cur = _t_text_from_tag(spans[s["tOccurrence"]].group())
                    else:
                        cur = None
                except Exception:
                    cur = None
                if cur is not None and not s.get("hasTab") and cur == new_value:
                    continue
                raw, t_end, new_len = _apply_replacement(
                    raw, s["tOccurrence"], new_value, bool(s.get("hasTab"))
                )
                raw, _ = trim_lineseg(raw, t_end, new_len)
                applied.append(s.get("label"))
            sect[sf] = raw

        return _write_package(src, sect, Path(tmpdir) / "output.hwpx")


def _write_package(src: Path, section_overrides: dict, out_path: Path) -> bytes:
    """원본 raw 항목을 그대로 두고, 바뀐 section만 무압축(STORED)으로 덮어쓴 zip 패키지 생성."""
    entries = read_raw_entries(src)
    overrides = {name: txt.encode("utf-8") for name, txt in section_overrides.items()}
    build_zip(entries, overrides, out_path)
    return out_path.read_bytes()
