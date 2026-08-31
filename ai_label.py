"""AI 기반 템플릿 자리(slot) 이름표 붙이기.

판단이 필요한 부분은 "채울 때"가 아니라 "템플릿 만들 때" 딱 한 번만 —
템플릿의 각 자리에 내용 기반 한글 이름표("문서번호", "수신", "본문-도입" 등)를
한 번 붙여두면, 그 다음부터 새 hwpx 생성은 {이름표: 새값} 딕셔너리로 순수
기계적 치환만 하면 된다(generate.py). 프로바이더 분기(qwen/claude)는
Unifold(~/projects/unifold/unifold/ai_filler.py, ai_extractor.py)에서 이미
검증된 패턴을 그대로 재사용.
"""
from __future__ import annotations

import json
import re

_QWEN_BASE_URL = "http://localhost:8001/v1"
_QWEN_MODEL = "Qwen/Qwen3-14B-AWQ"
_CLAUDE_MODEL = "claude-sonnet-4-6"

_SYSTEM_PROMPT = """당신은 한국어 공문서 구조 분석 전문가입니다.
문서 템플릿의 각 "자리"(slot)에 짧고 구체적인 한글 이름표를 붙여주세요.

원칙:
- 실제 내용을 보고 그 자리가 무엇을 의미하는지 판단하세요 (예: "새움터 2025-04-02" → "문서번호")
- 반복되는 자리(본문 문단 여러 개 등)는 순서를 살려 구분하세요 (예: "본문1", "본문2")
- 이름표는 2~8자 내외 한글 단어/구로, 사람이 봐도 바로 무슨 자리인지 알 수 있게
- 고정된 서식 문구(예: "이상과 같이 알려드립니다")도 그대로 이름표를 붙이세요 — 나중에
  값을 그대로 유지할지 바꿀지는 사람이 정합니다

반드시 아래 JSON 형식으로만 응답하세요. 설명 없이 JSON만:
{
  "slot_id": "이름표",
  "slot_id2": "이름표2"
}"""


def flatten_fillable_slots(structure: list) -> list[dict]:
    """template.structure에서 위치특정 가능한(tOccurrence not None) 자리만 골라
    평평한 리스트로 만든다. LLM 프롬프트는 깊이 중첩된 JSON보다 이런 평면
    목록에서 훨씬 안정적으로 응답한다."""
    slots = []
    for bi, block in enumerate(structure):
        if block.get("type") == "paragraph":
            if block.get("tOccurrence") is not None and (block.get("text") or "").strip():
                slots.append({
                    "slotId": f"p{bi}",
                    "role": block.get("style"),
                    "text": block.get("text", ""),
                })
        elif block.get("type") == "table":
            for ri, row in enumerate(block.get("cells", [])):
                for ci, cell in enumerate(row):
                    if cell.get("tOccurrence") is not None and (cell.get("text") or "").strip():
                        slots.append({
                            "slotId": f"t{bi}_{ri}_{ci}",
                            "role": cell.get("style"),
                            "text": cell.get("text", ""),
                        })
    return slots


def merge_labels_into_structure(structure: list, labels: dict[str, str]) -> list:
    """flatten_fillable_slots와 정확히 같은 slotId 규칙으로 라벨을 원래 구조에 되붙인다."""
    for bi, block in enumerate(structure):
        if block.get("type") == "paragraph":
            slot_id = f"p{bi}"
            if slot_id in labels:
                block["label"] = labels[slot_id]
        elif block.get("type") == "table":
            for ri, row in enumerate(block.get("cells", [])):
                for ci, cell in enumerate(row):
                    slot_id = f"t{bi}_{ri}_{ci}"
                    if slot_id in labels:
                        cell["label"] = labels[slot_id]
    return structure


def _build_user_message(slots: list[dict]) -> str:
    lines = []
    for s in slots:
        role = s.get("role") or ""
        text = (s.get("text") or "").replace("\n", " ")[:120]
        lines.append(f'- {s["slotId"]} (역할: {role}): "{text}"')
    return "다음은 문서 템플릿의 자리 목록입니다. 각 자리에 이름표를 붙여주세요:\n\n" + "\n".join(lines)


def _parse_json(content: str) -> dict:
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise ValueError(f"LLM 응답에서 JSON을 찾을 수 없습니다:\n{content[:300]}")
    return json.loads(match.group())


def _call_qwen(slots: list[dict]) -> dict:
    from openai import OpenAI
    client = OpenAI(base_url=_QWEN_BASE_URL, api_key="none")
    response = client.chat.completions.create(
        model=_QWEN_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_message(slots)},
        ],
        temperature=0.1,
        max_tokens=2048,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    content = response.choices[0].message.content or ""
    return _parse_json(content)


def _call_claude(slots: list[dict], api_key: str) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=_CLAUDE_MODEL,
        max_tokens=2048,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_message(slots)}],
    )
    content = response.content[0].text if response.content else ""
    return _parse_json(content)


def _dedupe_labels(label_map: dict, slot_order: list[str]) -> dict[str, str]:
    """같은 라벨이 여러 자리에 붙으면 번호를 붙여 구분(예: 본문 → 본문1, 본문2)."""
    seen: dict[str, int] = {}
    result = {}
    for slot_id in slot_order:
        label = str(label_map.get(slot_id) or slot_id).strip() or slot_id
        if label in seen:
            seen[label] += 1
            result[slot_id] = f"{label}{seen[label]}"
        else:
            seen[label] = 1
            result[slot_id] = label
    return result


def label_slots(slots: list[dict], provider: str = "qwen", api_key: str = "") -> dict[str, str]:
    """slots: [{slotId, role, text}, ...] → {slotId: 라벨} (중복 라벨은 자동 번호 부여)"""
    if not slots:
        return {}
    if provider == "claude":
        raw_labels = _call_claude(slots, api_key)
    else:
        raw_labels = _call_qwen(slots)
    return _dedupe_labels(raw_labels, [s["slotId"] for s in slots])
