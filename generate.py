"""라벨 붙은 템플릿 + {라벨: 새값} 딕셔너리 → 실제 hwpx 파일 생성.

판단(이 자리가 뭘 의미하는지)은 ai_label.py가 템플릿을 만들 때 이미 끝냈다는
전제 하에, 여기는 순수 기계적 치환만 한다 — 원본 hwpx의 zip 구조를
raw_zip_patch로 무손실 재포장하면서, 값이 바뀌는 문단의 <hp:t> 안 텍스트만
교체하고 그 문단의 <hp:lineseg> 줄바꿈 캐시를 새 텍스트 길이에 맞게 정리한다
(2026-08-30 HL만도 hwpx로 실증된 두 안전장치 그대로 재사용).

원본과 자리 개수 자체가 다른 경우(문단이 늘거나 표 행이 달라지는 등)는
범위 밖 — 그건 lineseg를 새로 계산해야 하는 구조편집이라
hwpx-style-transplant 스킬 영역으로 남겨둔다.
"""
from __future__ import annotations

import io
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


class GenerateError(ValueError):
    pass


def _rendered_text_length(tag_xml: str) -> int:
    """<hp:t>...</hp:t> 조각 하나를 독립 파싱해 렌더링 글자수를 센다 (tab=1글자,
    app.py의 t_element_text와 동일한 계산 규칙)."""
    wrapped = f"<root {_HP_NS}>{tag_xml}</root>"
    t_el = ET.fromstring(wrapped)[0]
    parts = [t_el.text or ""]
    for child in t_el:
        if child.tag.endswith("}tab"):
            parts.append("\t")
        parts.append(child.tail or "")
    return len("".join(parts))


def _apply_replacement(raw: str, t_occurrence: int, new_value: str, has_tab: bool) -> tuple[str, int, int]:
    """raw 안에서 t_occurrence번째(0-based) <hp:t> 태그를 새 값으로 교체한다.
    has_tab이면 마지막 <hp:tab/> 뒤 내용만 바꾸고 앞부분(라벨+tab)은 원본 그대로 보존.
    반환: (새 raw, 교체된 태그 끝 위치, 새 렌더링 글자수) — 뒤 둘은 trim_lineseg 인자."""
    spans = list(_T_SPAN_RE.finditer(raw))
    if t_occurrence >= len(spans):
        raise GenerateError(f"<hp:t> occurrence {t_occurrence}을(를) 찾을 수 없습니다 (문서에 {len(spans)}개뿐)")
    m = spans[t_occurrence]
    old_tag = m.group()
    escaped_value = escape(new_value)

    if has_tab and not old_tag.endswith("/>"):
        tab_matches = list(_TAB_RE.finditer(old_tag))
    else:
        tab_matches = []

    if tab_matches:
        prefix_end = tab_matches[-1].end()
        prefix = old_tag[:prefix_end]
        new_tag = prefix + escaped_value + "</hp:t>"
        new_len = _rendered_text_length(prefix + "</hp:t>") + len(new_value)
    else:
        new_tag = f"<hp:t>{escaped_value}</hp:t>"
        new_len = len(new_value)

    new_raw = raw[:m.start()] + new_tag + raw[m.end():]
    new_end = m.start() + len(new_tag)
    return new_raw, new_end, new_len


def _iter_slots(structure: list):
    """template.structure를 순회하며 치환 가능한 자리(문단 또는 표 셀)를 전부 낸다."""
    for block in structure:
        if block.get("type") == "paragraph":
            yield block
        elif block.get("type") == "table":
            for row in block.get("cells", []):
                for cell in row:
                    yield cell


def generate_hwpx(original_bytes: bytes, template: dict, values: dict[str, str]) -> bytes:
    """template: /api/template + /api/label 출력물(각 자리에 label 필드 포함).
    values: {라벨: 새 텍스트} — 없는 라벨의 자리는 원본 값 그대로 유지.
    반환: 새 hwpx 파일 바이트."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = Path(tmpdir) / "original.hwpx"
        src_path.write_bytes(original_bytes)

        with zipfile.ZipFile(src_path) as zf:
            names = zf.namelist()
            section_texts = {
                name: zf.read(name).decode("utf-8")
                for name in names
                if re.match(r"[Cc]ontents/section\d+\.xml$", name)
            }

        by_section: dict[str, list[dict]] = {}
        for slot in _iter_slots(template.get("structure", [])):
            label = slot.get("label")
            if not label or label not in values:
                continue
            if slot.get("tOccurrence") is None or not slot.get("sectionFile"):
                continue
            by_section.setdefault(slot["sectionFile"], []).append(slot)

        applied = []
        for section_file, slots in by_section.items():
            raw = section_texts.get(section_file)
            if raw is None:
                continue
            # occurrence 내림차순으로 처리 — 텍스트 길이 변화가 이후(occurrence
            # 인덱스가 더 큰) 자리를 찾는 데 영향을 주지 않도록 안전하게 뒤에서부터.
            for slot in sorted(slots, key=lambda s: s["tOccurrence"], reverse=True):
                new_value = values[slot["label"]]
                raw, t_end, new_len = _apply_replacement(
                    raw, slot["tOccurrence"], new_value, slot.get("hasTab", False)
                )
                raw, _ = trim_lineseg(raw, t_end, new_len)
                applied.append(slot["label"])
            section_texts[section_file] = raw

        entries = read_raw_entries(src_path)
        overrides = {name: text.encode("utf-8") for name, text in section_texts.items()}
        out_path = Path(tmpdir) / "output.hwpx"
        build_zip(entries, overrides, out_path)
        return out_path.read_bytes()
