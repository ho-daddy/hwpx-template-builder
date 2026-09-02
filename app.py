"""
HWPX → 재사용 템플릿 변환 웹앱 (FastAPI 단일 파일)

- HWPX(zip+xml)를 표준 라이브러리(zipfile, xml.etree.ElementTree)로 직접 파싱
- (paraPrIDRef, charPrIDRef, inTable) 키로 스타일 클러스터 자동 생성
- 휴리스틱으로 역할 이름(제목/본문/목록/표머리글 등) 제안
- POST /api/template 에서 최종 템플릿 JSON 생성
- POST /api/label 에서 각 자리에 AI가 내용 기반 한글 이름표를 붙임(1회성 판단)
- POST /api/templates 로 저장 후, POST /api/templates/{id}/generate 로
  {라벨: 새값} 딕셔너리만 받아 순수 기계적으로 새 hwpx 생성
"""
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

import ai_label
import db
from generate import GenerateError, build_template_package, generate_hwpx

app = FastAPI(title="HWPX Template Builder")
BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
db.init_db()

# ---------------------------------------------------------------------------
# 방어적 XML 헬퍼
# ---------------------------------------------------------------------------

def localname(tag):
    """태그에서 네임스페이스를 제거한 로컬 이름.
    (네임스페이스 URI가 파일 버전에 따라 달라질 수 있어 로컬 이름으로만 매칭)"""
    if not isinstance(tag, str):
        return None
    if "}" in tag:
        return tag.split("}", 1)[1]
    if ":" in tag:
        return tag.split(":", 1)[1]
    return tag


def attr_any(elem, names, default=None):
    """후보 속성명 리스트를 순서대로 시도해 첫 번째로 존재하는 값을 반환.
    (정확한 속성명이 불확실한 부분을 코드로 방어적으로 처리)"""
    for n in names:
        v = elem.get(n)
        if v is not None and v != "":
            return v
    return default


def child_any(elem, names):
    """후보 자식 태그(로컬 이름) 리스트를 순서대로 시도해 첫 발견 요소를 반환.
    (정확한 자식 태그명이 불확실한 부분을 코드로 방어적으로 처리)"""
    for n in names:
        for c in elem:
            if localname(c.tag) == n:
                return c
    return None


def to_pt(v, default=0.0):
    """HWPX의 1/100pt 단위 값을 pt로 변환."""
    try:
        return round(int(str(v).strip()) / 100.0, 2)
    except (TypeError, ValueError):
        return default


def to_int(v, default=1):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# header.xml 파싱 (공유 스타일 정의)
# ---------------------------------------------------------------------------

def extract_char_pr(el):
    d = {
        "id": to_int(el.get("id"), -1),
        "heightPt": to_pt(attr_any(el, ["height", "fontSize", "size"])),
        "color": attr_any(el, ["textColor", "color", "foreColor"]),
        # 볼드/이탤릭 등이 속성이자 자식 태그일 수 있어 둘 다 시도 (확실치 않아 코드로 방어적으로 처리)
        "bold": child_any(el, ["bold"]) is not None or attr_any(el, ["bold"]) in ("1", "true"),
        "italic": child_any(el, ["italic"]) is not None or attr_any(el, ["italic"]) in ("1", "true"),
        "underline": child_any(el, ["underline"]) is not None,
        "strikeout": child_any(el, ["strikeout"]) is not None,
        "font": None,
    }
    fr = child_any(el, ["fontRef", "font", "fontFace"])
    if fr is not None:
        d["font"] = attr_any(fr, ["name", "font", "fontFamily", "faceName"])
    if d["font"] is None:
        d["font"] = attr_any(el, ["font", "fontFamily", "fontName"])
    return d


def extract_para_pr(el):
    d = {"id": to_int(el.get("id"), -1)}
    # 정렬/들여쓰기/줄간격이 속성이자 자식 태그일 수 있어 둘 다 시도 (확실치 않아 코드로 방어적으로 처리)
    al = child_any(el, ["align", "alignment"])
    d["align"] = (attr_any(al, ["value", "type", "mode"]) if al is not None else None) \
        or attr_any(el, ["align", "alignment"], "left")
    ind = child_any(el, ["indent", "indentation"])
    d["indentLeftPt"] = to_pt(attr_any(ind, ["left", "leftIndent", "start"]) if ind is not None
                              else attr_any(el, ["indentLeft", "leftIndent"]))
    d["indentRightPt"] = to_pt(attr_any(ind, ["right", "rightIndent", "end"]) if ind is not None
                               else attr_any(el, ["indentRight", "rightIndent"]))
    lp = child_any(el, ["linePitch", "lineSpacing"])
    d["linePitchPt"] = to_pt(attr_any(lp, ["value", "pitch"]) if lp is not None
                             else attr_any(el, ["linePitch", "lineSpacing"]))
    sb = child_any(el, ["spaceBefore"])
    d["spaceBeforePt"] = to_pt(attr_any(sb, ["value"]) if sb is not None else attr_any(el, ["spaceBefore"]))
    sa = child_any(el, ["spaceAfter"])
    d["spaceAfterPt"] = to_pt(attr_any(sa, ["value"]) if sa is not None else attr_any(el, ["spaceAfter"]))
    d["list"] = child_any(el, ["listPr", "list", "numbering", "numPr"]) is not None
    return d


def parse_header(zf):
    header_name = None
    for n in zf.namelist():
        if n.lower().endswith("header.xml"):
            header_name = n
            if n.lower() == "contents/header.xml":
                break
    if header_name is None:
        raise ValueError("header.xml을 찾을 수 없습니다")
    root = ET.fromstring(zf.read(header_name))
    char_prs, para_prs = {}, {}
    for el in root.iter():
        ln = localname(el.tag)
        if ln == "charPr":
            char_prs[to_int(el.get("id"), -1)] = extract_char_pr(el)
        elif ln == "paraPr":
            para_prs[to_int(el.get("id"), -1)] = extract_para_pr(el)
    return char_prs, para_prs


# ---------------------------------------------------------------------------
# section*.xml 파싱 (본문: 문단/표 순회)
# ---------------------------------------------------------------------------

class Doc:
    def __init__(self):
        self.paragraphs = []   # 문서 순서대로 모든 문단 (표 안 문단 포함)
        self.structure = []    # 문서 골격 (상위 문단/표 블록 순서)
        self.tables = {}
        self._table_seq = 0

    def add_para(self, para_pr_ref, char_pr_ref, text, in_table,
                 t_occurrence=None, has_tab=False, section_file=None):
        self.paragraphs.append({
            "index": len(self.paragraphs),
            "text": text,
            "paraPrIDRef": para_pr_ref,
            "charPrIDRef": char_pr_ref,
            "inTable": in_table,
            # generate 단계(새 hwpx 생성)에서 이 문단의 <hp:t>를 raw XML에서 다시
            # 찾기 위한 위치정보. tOccurrence는 이 섹션파일 안에서 몇 번째 <hp:t>
            # 태그인지(런 1개짜리 문단만 유일하게 특정 가능 — 여러 런이면 None).
            "tOccurrence": t_occurrence,
            "hasTab": has_tab,
            "sectionFile": section_file,
        })
        return len(self.paragraphs) - 1


def t_element_text(t_el):
    """<hp:t> 하나의 전체 텍스트를 복원한다. HWPX는 tab처럼 텍스트 중간에 끼는
    인라인 컨트롤을 자식 요소로 넣는데, 그 뒤에 이어지는 텍스트는 ElementTree
    기준으로 그 자식의 .tail에 담기고 t_el.text에는 안 잡힌다 — 그래서 t_el.text만
    읽으면 tab 뒤 내용(예: "문서번호<tab/>새움터 2025-04-02"에서 탭 뒤 값)이
    통째로 사라짐(2026-08-30 새움터 공문양식 실사례로 발견). 자식들의 tail까지
    순서대로 이어붙여 복원하고, tab은 실제 표시대로 "\t"로 치환한다."""
    parts = [t_el.text or ""]
    for child in t_el:
        if localname(child.tag) == "tab":
            parts.append("\t")
        parts.append(child.tail or "")
    return "".join(parts)


def has_tab_child(t_el):
    return any(localname(c.tag) == "tab" for c in t_el)


def para_text_and_runs(p_el, counter):
    """문단 내 run 텍스트를 모으고 charPrIDRef별 글자 수를 세어 대표 run을 정한다.
    HWPX는 run 자체가 아니라 run의 자식 <hp:t>에 텍스트를 담으므로 그쪽에서 읽는다.
    p_el의 직계 run 자식만 본다(iter() 전체탐색 X) — 문단 안에 표가 중첩된 경우
    표 셀 내부의 run까지 딸려와 문단 텍스트에 섞이는 것을 방지한다.

    counter: [int] 형태의 공유 카운터 — 이 섹션파일 안에서 만난 모든 <hp:t>
    태그(자기닫힘 포함) 순서를 0부터 셈. ElementTree 순회는 raw XML의 좌→우
    등장 순서와 항상 일치하므로, 이 번호가 나중에 generate 단계에서 raw 문자열을
    regex로 다시 스캔했을 때의 occurrence 인덱스와 정확히 대응한다.
    문단이 <hp:t>를 정확히 1개만 가질 때만(런 1개) 유일하게 특정 가능해 tOccurrence를
    반환하고, 0개/2개 이상이면 None(치환 대상에서 제외 — 다음 개발 범위)."""
    parts, run_counts = [], {}
    occurrences = []
    for el in p_el:
        if localname(el.tag) != "run":
            continue
        t_el = child_any(el, ["t"])
        if t_el is None:
            continue
        occurrences.append((counter[0], has_tab_child(t_el)))
        counter[0] += 1
        t = t_element_text(t_el)
        if not t:
            continue
        parts.append(t)
        ref = attr_any(el, ["charPrIDRef", "charPrRef", "charPrId"])
        ref = ref if ref is not None else "0"
        run_counts[ref] = run_counts.get(ref, 0) + len(t)
    if len(occurrences) == 1:
        t_occurrence, has_tab = occurrences[0]
    else:
        t_occurrence, has_tab = None, False
    return "".join(parts), run_counts, t_occurrence, has_tab


def extract_table(tbl_el, doc, record=True, counter=None, section_file=None):
    rows = []
    for tr in tbl_el:
        if localname(tr.tag) != "tr":
            continue
        row = []
        for tc in tr:
            if localname(tc.tag) != "tc":
                continue
            # 셀 병합 속성명은 파일에 따라 다를 수 있어 후보를 순서대로 시도 (방어적 처리)
            rs = to_int(attr_any(tc, ["rowSpan", "rowSpanCount", "vMerge", "rowSpanValue"]), 1)
            cs = to_int(attr_any(tc, ["colSpan", "colSpanCount", "hMerge", "colSpanValue"]), 1)
            cell_idx, best_len = None, -1
            for el in tc.iter():
                if localname(el.tag) != "p":
                    continue
                text, runs, t_occ, has_tab = para_text_and_runs(el, counter)
                dom = max(runs, key=runs.get) if runs else "0"
                idx = doc.add_para(
                    attr_any(el, ["paraPrIDRef", "paraPrRef", "paraPrId"]),
                    dom, text, True, t_occ, has_tab, section_file,
                )
                # 셀 대표 문단 = 텍스트가 가장 긴 문단 (나머지 문단도 클러스터링에는 포함)
                if len(text) > best_len:
                    best_len, cell_idx = len(text), idx
            row.append({"rowSpan": rs, "colSpan": cs, "paraIndex": cell_idx})
        rows.append(row)
    cols = max((len(r) for r in rows), default=0)
    block = {"type": "table", "rows": len(rows), "cols": cols, "cells": rows}
    if record:
        block["id"] = f"t{doc._table_seq}"
        doc.tables[block["id"]] = {"rows": len(rows), "cols": cols}
        doc._table_seq += 1
    return block


def _find_embedded_tbls(p_el):
    """문단이 run 안에 인라인으로 직접 품은 <hp:tbl>을 문서 순서대로 반환.
    HWPX는 표를 문단 안 run에 중첩한다(sec>p>run>tbl)."""
    found = []
    for run in p_el:
        if localname(run.tag) != "run":
            continue
        for g in run:
            if localname(g.tag) == "tbl":
                found.append(g)
    return found


def _count_direct_run_ts(p_el, counter):
    """문단의 run이 직접 지닌 <hp:t>(그림/표가 아닌 텍스트)를 카운터로만 센다.
    컨테이너 문단의 '표 뒤 <hp:t/> 마무리' 같은 빈 t를 raw 위치에 맞춰 세는 데 쓰인다.
    (표 안 셀 문단은 extract_table이 이미 counter를 진행시킨다.)"""
    for run in p_el:
        if localname(run.tag) != "run":
            continue
        for g in run:
            if localname(g.tag) == "t":
                counter[0] += 1


def _para_visible_text(p_el):
    """문단이 (그림/표 같은 인라인 개체와 무관하게) 실제로 이룬 가시 텍스트.
    빈 <hp:t/>는 가시 텍스트로 치지 않는다."""
    parts = []
    for run in p_el:
        if localname(run.tag) != "run":
            continue
        for g in run:
            if localname(g.tag) == "t":
                parts.append(g.text or "")
    return "".join(parts)


def walk(el, in_table, doc, counter, section_file):
    for child in el:
        ln = localname(child.tag)
        if ln == "p":
            own_tbls = _find_embedded_tbls(child)
            if own_tbls and not in_table and not _para_visible_text(child).strip():
                # --- 인라인 표 컨테이너 문단 처리 ---
                # 컨테이너 문단(sec>p>run>tbl)은 fillable 본문이 아니라 표를 담는 골격
                # 일 뿐이다. 그런데 그 run의 <hp:t/> 마무리(표 뒤)는 ElementTree가
                # "앞"에서 세는 반면 실제 raw 문자열에서는 표 전체가 끝난 뒤 위치해,
                # 순회 카운터를 그대로 쓰면 표 셀 index가 +1 밀린다(금속노조성명서 첫
                # 표에서 확인). 해결: 1) 먼저 표 셀들이 (앞에 아무 것도 안 세고) raw
                # 순서와 일치하게 index를 소비하게 한 뒤, 2) 컨테이너 자신의 표 뒤
                # 마무리 <hp:t/>를 raw 위치에 맞게 센다. 이러면 셀은 0,1,2..., 이어지는
                # 본문도 어긋나지 않는다.
                for tb in own_tbls:
                    block = extract_table(tb, doc, record=True,
                                          counter=counter, section_file=section_file)
                    doc.structure.append(block)
                _count_direct_run_ts(child, counter)
                continue
            text, runs, t_occ, has_tab = para_text_and_runs(child, counter)
            dom = max(runs, key=runs.get) if runs else "0"
            idx = doc.add_para(
                attr_any(child, ["paraPrIDRef", "paraPrRef", "paraPrId"]),
                dom, text, in_table, t_occ, has_tab, section_file,
            )
            if not in_table:
                doc.structure.append({"type": "paragraph", "paraIndex": idx})
            # HWPX는 표를 문단 안 run에 중첩한다(sec>p>run>tbl) — run 자식까지 내려가 표를 찾는다.
            for tb in own_tbls:
                block = extract_table(tb, doc, record=not in_table,
                                       counter=counter, section_file=section_file)
                if not in_table:
                    doc.structure.append(block)
        elif ln == "tbl":
            block = extract_table(child, doc, record=not in_table,
                                   counter=counter, section_file=section_file)
            if not in_table:
                doc.structure.append(block)
        else:
            walk(child, in_table, doc, counter, section_file)


# ---------------------------------------------------------------------------
# 클러스터링 + 역할 휴리스틱
# ---------------------------------------------------------------------------

ROLE_KO = {
    "title": "제목", "subtitle": "부제목", "heading": "소제목", "body": "본문",
    "list_item": "목록", "table_header": "표 머리글", "table_cell": "표 셀",
    "caption": "캡션", "footnote": "주석", "text": "일반 텍스트", "other": "기타",
}

BULLET_RE = re.compile(
    r"^\s*(?:"
    r"[•·◦▪◢◣‣∙*+-]"
    r"|\d+[\.、)）:]"
    r"|\(\d+\)"
    r"|[①②③④⑤⑥⑦⑧⑨⑩]"
    r"|[（(]\s*\d+\s*[)）]"
    r")\s"
)


def guess_role(c, doc, body_h, body_id):
    ch = c["charPr"] or {}
    pa = c["paraPr"] or {}
    h = ch.get("heightPt") or 0
    bold = bool(ch.get("bold"))
    n = len(c["members"])
    first = c["members"][0] if c["members"] else 0
    texts = [doc.paragraphs[i]["text"] for i in c["members"]]
    nonempty = [t for t in texts if t.strip()]
    listish = bool(pa.get("list"))
    if not listish and nonempty:
        hits = sum(1 for t in nonempty if BULLET_RE.match(t))
        listish = hits * 2 >= len(nonempty)
    if c["inTable"]:
        return "table_header" if bold else "table_cell"
    if listish:
        return "list_item"
    if h and body_h:
        if h >= body_h * 1.3 and n <= 5 and first <= 10:
            return "title"
        if h >= body_h * 1.15 and n <= 12:
            return "heading"
        if h <= body_h * 0.85:
            return "caption" if h >= body_h * 0.7 else "footnote"
    if bold and (pa.get("align") or "").lower() == "center":
        return "subtitle"
    if c["id"] == body_id:
        return "body"
    return "text"


def assign_roles(clusters, doc):
    # 본문 기준 글자크기 = 표 제외 클러스터에서 문단 수 가중 모수
    heights = {}
    for c in clusters:
        if c["inTable"]:
            continue
        h = (c["charPr"] or {}).get("heightPt")
        if h:
            heights[h] = heights.get(h, 0) + len(c["members"])
    body_h = max(heights, key=heights.get) if heights else 11.0

    body_id, best = None, -1
    for c in clusters:
        if not c["inTable"] and len(c["members"]) > best:
            best, body_id = len(c["members"]), c["id"]

    for c in clusters:
        base = guess_role(c, doc, body_h, body_id)
        c["_baseRole"] = base
        c["role"] = base
        c["roleKorean"] = ROLE_KO.get(base, "기타")

    # 이름 충돌 시 heading, heading2, heading3 ...
    used = {}
    for c in clusters:
        base = c["_baseRole"]
        if base in used:
            used[base] += 1
            c["role"] = f"{base}{used[base]}"
        else:
            used[base] = 1


def build_clusters(doc, char_prs, para_prs):
    # 핵심: 같은 (paraPrIDRef, charPrIDRef, inTable) 조합 = 저작자가 지정한 동일 스타일
    groups, order = {}, []
    for p in doc.paragraphs:
        key = (p["paraPrIDRef"], p["charPrIDRef"], p["inTable"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(p["index"])
    clusters = []
    for ppr, cpr, in_tbl in order:
        clusters.append({
            "id": f"c{len(clusters)}",
            "paraPrIDRef": ppr,
            "charPrIDRef": cpr,
            "inTable": in_tbl,
            "members": groups[(ppr, cpr, in_tbl)],
            "charPr": char_prs.get(to_int(cpr, -1)),
            "paraPr": para_prs.get(to_int(ppr, -1)),
        })
    assign_roles(clusters, doc)
    return clusters


# ---------------------------------------------------------------------------
# 전체 분석
# ---------------------------------------------------------------------------

def analyze_hwpx(data: bytes, filename: str):
    zf = zipfile.ZipFile(io.BytesIO(data))
    char_prs, para_prs = parse_header(zf)
    doc = Doc()
    sec_names = [n for n in zf.namelist()
                 if re.match(r"contents/section\d+\.xml$", n, re.IGNORECASE)]
    if not sec_names:
        sec_names = [n for n in zf.namelist()
                     if re.search(r"section\d*\.xml$", n, re.IGNORECASE)]
    if not sec_names:
        raise ValueError("section*.xml 본문 파일을 찾을 수 없습니다")
    sec_names.sort(key=lambda n: to_int(re.search(r"section(\d+)", n, re.IGNORECASE).group(1), 0))
    for n in sec_names:
        root = ET.fromstring(zf.read(n))
        body = next((el for el in root.iter() if localname(el.tag) == "body"), None)
        counter = [0]  # 섹션파일마다 <hp:t> occurrence 번호를 0부터 새로 셈
        walk(body if body is not None else root, False, doc, counter, n)
    clusters = build_clusters(doc, char_prs, para_prs)
    return {
        "meta": {
            "sourceFile": filename,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "paragraphCount": len(doc.paragraphs),
            "tableCount": len(doc.tables),
            "charPrCount": len(char_prs),
            "paraPrCount": len(para_prs),
        },
        "paragraphs": doc.paragraphs,
        "clusters": clusters,
        "structure": doc.structure,
        "tables": doc.tables,
    }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    data = await file.read()
    name = file.filename or "upload.hwpx"
    if data[:4] == b"\xd0\xcf\x11\xe0":
        raise HTTPException(400, "구형 .hwp(OLE2) 파일은 이번 버전에서 지원하지 않습니다. .hwpx로 변환해 주세요.")
    if data[:2] != b"PK":
        raise HTTPException(400, "HWPX(ZIP) 파일이 아닙니다. .hwpx 파일을 업로드해 주세요.")
    try:
        return analyze_hwpx(data, name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"HWPX 파싱 실패: {type(e).__name__}: {e}")


@app.post("/api/template")
def make_template(payload: dict):
    """사용자 검토/수정 결과를 받아 최종 템플릿 JSON(hwpx-template/1.0) 생성."""
    clusters = payload.get("clusters") or []
    paragraphs = payload.get("paragraphs") or []
    structure = payload.get("structure") or []
    meta = payload.get("meta") or {}
    pmap = {p["index"]: p for p in paragraphs}
    para_to_cluster = {}
    for c in clusters:
        for i in c.get("members", []):
            para_to_cluster[i] = c["id"]

    styles = []
    for c in clusters:
        members = c.get("members", [])
        samples = []
        for i in members:
            t = (pmap.get(i) or {}).get("text", "").strip()
            if t and t not in samples:
                samples.append(t[:100])
            if len(samples) >= 3:
                break
        styles.append({
            "id": c.get("id"),
            "role": c.get("role"),
            "roleKorean": c.get("roleKorean"),
            "inTable": c.get("inTable"),
            "charPr": c.get("charPr"),
            "paraPr": c.get("paraPr"),
            "occurrences": len(members),
            "samples": samples,
        })

    out_structure = []
    for block in structure:
        btype = block.get("type")
        if btype == "paragraph":
            i = block.get("paraIndex")
            p = pmap.get(i) or {}
            out_structure.append({
                "type": "paragraph",
                "style": para_to_cluster.get(i),
                "text": p.get("text", ""),
                "paraIndex": i,
                # generate 단계에서 이 자리를 다시 찾기 위한 위치정보 (analyze 단계에서 계산됨)
                "tOccurrence": p.get("tOccurrence"),
                "hasTab": p.get("hasTab", False),
                "sectionFile": p.get("sectionFile"),
            })
        elif btype == "table":
            cells = []
            for row in block.get("cells", []):
                crow = []
                for cell in row:
                    i = cell.get("paraIndex")
                    p = (pmap.get(i) or {}) if i is not None else {}
                    crow.append({
                        "style": para_to_cluster.get(i) if i is not None else None,
                        "text": p.get("text", "") if i is not None else "",
                        "paraIndex": i,
                        "tOccurrence": p.get("tOccurrence") if i is not None else None,
                        "hasTab": p.get("hasTab", False) if i is not None else False,
                        "sectionFile": p.get("sectionFile") if i is not None else None,
                        "rowSpan": cell.get("rowSpan", 1),
                        "colSpan": cell.get("colSpan", 1),
                    })
                cells.append(crow)
            out_structure.append({
                "type": "table",
                "rows": block.get("rows"),
                "cols": block.get("cols"),
                "cells": cells,
            })

    return {
        "meta": {
            **meta,
            "schema": "hwpx-template/1.0",
            "generator": "hwpx-template-builder/1.0",
            "templateGeneratedAt": datetime.now(timezone.utc).isoformat(),
        },
        "styles": styles,
        "structure": out_structure,
    }


@app.post("/api/label")
def label_template(payload: dict):
    """template(=/api/template 출력물)의 각 자리에 AI가 내용 기반 한글
    이름표를 붙여 돌려준다. 이 판단은 템플릿당 1회만 필요 — generate는
    이 결과를 그대로 저장해뒀다가 기계적으로만 값을 채운다."""
    structure = payload.get("structure") or []
    provider = payload.get("provider") or "qwen"
    api_key = payload.get("apiKey") or ""
    slots = ai_label.flatten_fillable_slots(structure)
    try:
        labels = ai_label.label_slots(slots, provider=provider, api_key=api_key)
    except Exception as e:
        raise HTTPException(502, f"AI 라벨링 실패({provider}): {type(e).__name__}: {e}")
    structure = ai_label.merge_labels_into_structure(structure, labels)
    return {**payload, "structure": structure, "labelCount": len(labels)}


@app.post("/api/templates")
async def create_template(
    name: str = Form(...),
    template: str = Form(...),
    file: UploadFile = File(...),
):
    """라벨 붙은 template(JSON) + 원본 hwpx 파일을 받아 저장한다.

    핵심: 원본을 그대로 두지 않고, 각 라벨 자리를 placeholder로 치환한 "템플릿화된
    패키지"(build_template_package)를 만들어 함께 저장한다. 따라서 템플릿은 "원본과
    동일한 형식(xml 포함 패키지)"이며, generate 단계는 이 패키지에서 placeholder를
    값으로 교체만 하면 된다. 원본 바이트(original_hwpx)도 보존해 placeholder에 담기
    어려운 자리(복수 run 등) 복원에 대비한다."""
    import json as _json
    try:
        template_obj = _json.loads(template)
    except _json.JSONDecodeError as e:
        raise HTTPException(400, f"template이 올바른 JSON이 아닙니다: {e}")
    data = await file.read()

    # 원본 패키지에서 라벨 자리만 placeholder로 치환한 "템플릿화된 패키지" 생성.
    # 템플릿 자체가 원본과 같은 형식의 hwpx 패키지가 된다.
    try:
        template_hwpx = build_template_package(data, template_obj)
    except Exception as e:
        raise HTTPException(500, f"템플릿 패키지 생성 실패: {type(e).__name__}: {e}")

    template_id = db.save_template(
        name, file.filename or "", template_obj, data, template_hwpx
    )
    return {"id": template_id, "name": name}


@app.get("/api/templates")
def list_templates():
    return db.list_templates()


@app.get("/api/templates/{template_id}")
def get_template(template_id: int):
    row = db.get_template(template_id)
    if row is None:
        raise HTTPException(404, "템플릿을 찾을 수 없습니다")
    # BLOB 두 컬럼(원본/템플릿화된 패키지)은 큰 바이너리라 JSON 응답에서 제외한다.
    # 프론트는 여기서 template 메타/구조만 필요하지 바이너리 패키지는 쓰지 않는다.
    row.pop("original_hwpx", None)
    row.pop("template_hwpx", None)
    return row


@app.delete("/api/templates/{template_id}")
def remove_template(template_id: int):
    if not db.delete_template(template_id):
        raise HTTPException(404, "템플릿을 찾을 수 없습니다")
    return {"deleted": template_id}


@app.post("/api/templates/{template_id}/generate")
def generate_from_template(template_id: int, payload: dict):
    """payload: {"values": {"라벨": "새 텍스트", ...}} → 새 hwpx 파일 응답.

    저장된 템플릿은 이미 "원본과 동일한 형식(xml 포함 패키지)"이고 각 라벨 자리가
    placeholder로 치환되어 있으므로, generate는 그 placeholder를 값으로만 교체한다.
    """
    row = db.get_template(template_id)
    if row is None:
        raise HTTPException(404, "템플릿을 찾을 수 없습니다")
    values = payload.get("values") or {}

        # 반드시 원본 preserved 패키지(original_hwpx)를 기준으로 생성한다.
    # template_hwpx(placeholder가 박힌 패키지)는 저장/다운로드용 산출물일 뿐,
    # placeholder를 박는 순간 문단이 짧아지며 그 <hp:lineseg>(줄바꿈 캐시)가 잘리고,
    # 손상된 파일에 원본과 같은 값으로 다시 메워도 잘린 캐시가 복구되지 않아
    # 표 셀 순서 밀림/텍스트 겹침이 생긴다. generate_hwpx는 원본 위에서 slot을
    # 찾아 치환하며, 값이 이미 원본과 같으면(동일 내용 재생성) 해당 문단을 아예
    # 건드리지 않는다 → 산출물이 원본과 똑같아진다.
    template_bytes = row.get("original_hwpx")
    if not template_bytes:
        raise HTTPException(500, "저장된 원본 패키지가 없습니다")

    try:
        out_bytes = generate_hwpx(template_bytes, row["template"], values)
    except GenerateError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"hwpx 생성 실패: {type(e).__name__}: {e}")
    # 파일명에 한글이 섞이면 HTTP 헤더가 latin-1 인코딩이라 그대로 못 들어감 —
    # RFC 5987 filename*=UTF-8''... 형식으로 퍼센트인코딩하고, ascii 폴백도 같이 준다.
    from urllib.parse import quote
    filename = f"{row['name']}.hwpx"
    ascii_fallback = "".join(c if ord(c) < 128 else "_" for c in filename) or "template.hwpx"
    return Response(
        content=out_bytes,
        media_type="application/vnd.hancom.hwpx",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_fallback}"; '
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )


if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC / "index.html"))


if __name__ == "__main__":
    import uvicorn
