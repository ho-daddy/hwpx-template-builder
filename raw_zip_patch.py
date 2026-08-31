# -*- coding: utf-8 -*-
"""
HWPX(zip 기반)를 raw XML 편집한 뒤 real 한글에서 "문서가 손상되었거나 변조되었을
가능성이 있습니다 — 문서 보안 설정을 낮음으로 설정해야 합니다" 경고 없이 열리게
만드는 데 필요한 두 가지 실증된 조치 (2026-08-30, HL만도 hwpx로 다단계 이분탐색
격리 후 확정). 자세한 재현 과정은 hwpx-style-transplant 스킬 참고.

1) build_zip() — zip 압축 지문 불일치 회피
   Python zipfile.writestr()는 내용이 안 바뀐 항목(이미지·header.xml 등)까지 자기
   zlib으로 재압축해버려서, 압축된 바이트 자체가 원본과 달라짐(no-op rezip으로도
   파일 크기가 5만 바이트 넘게 줄어듦 — 내용은 CRC32 동일한데 압축 바이트만 다름).
   더 결정적으로: 원본 zip local header의 general-purpose flag가 "Fast 압축"이라고
   주장하는데 실제로는 Python이 다른 강도로 압축한 스트림이 들어있는 "라벨-실제
   불일치" 자체를 한글이 감지해 위변조로 판단하는 것으로 실증 확인됨(내용이 100%
   동일해도 재현됨, 무압축 no-op으로는 재현 안 됨). 대응: 안 바뀐 항목은 원본 압축
   바이트를 그대로(raw) 복사하고, 실제로 바뀌는 항목만 STORED(무압축)로 써서 이
   라벨-실제 불일치 가능성 자체를 없앰.

2) trim_lineseg() — 줄바꿈 캐시(lineseg) 범위초과 회피
   문단 텍스트를 원문보다 짧게 바꾸면, 그 문단의 <hp:linesegarray> 안에 남아있는
   뒤쪽 <hp:lineseg> 항목들이 실제로는 존재하지 않는 글자 위치(textpos)를 가리키게
   됨(예: 원래 3줄짜리 문단을 2줄이면 충분한 텍스트로 줄였는데 "3번째 줄은 100번째
   글자부터" 캐시가 그대로 남는 경우). hwpx-style-transplant 스킬은 이걸 "리브레
   오피스로는 못 잡는 렌더링 오버랩 버그"로만 알고 있었는데, 실제로는 이 범위초과
   자체가 한글의 "손상/변조 가능성" 판정 조건 중 하나임을 이번에 실증 확인함(제목
   처럼 원래 한 줄짜리라 lineseg가 1개뿐인 문단은 아무리 짧게 바꿔도 통과, 여러 줄
   짜리 문단을 하나라도 포함하면 실패 — 이분탐색으로 확정). 대응: 텍스트를 넣은 뒤
   그 문단의 textpos가 새 텍스트 길이를 벗어나는 lineseg 항목을 제거(첫 항목은 항상
   유지).

두 조치를 함께 적용하면 압축 방식이 다르고 텍스트 길이가 원문보다 짧아도 경고 없이
열림 — 2026-08-30 HL만도 hwpx 샘플로 51개 문단 전체 치환 실전 검증 완료.
"""
import re
import struct
import zlib
import zipfile
from pathlib import Path

LOCAL_HDR_FMT = "<IHHHHHIIIHH"
LOCAL_HDR_SIZE = 30
LOCAL_SIG = 0x04034B50
CENTRAL_SIG = 0x02014B50
EOCD_SIG = 0x06054B50


def read_raw_entries(path):
    data = Path(path).read_bytes()
    zf = zipfile.ZipFile(path)
    entries = []
    for info in zf.infolist():
        off = info.header_offset
        sig, ver, flag, method, mtime, mdate, crc, csize, usize, fnlen, exlen = struct.unpack(
            LOCAL_HDR_FMT, data[off:off + LOCAL_HDR_SIZE]
        )
        assert sig == LOCAL_SIG, f"bad local header sig for {info.filename}: {sig:#x}"
        name_start = off + LOCAL_HDR_SIZE
        name = data[name_start:name_start + fnlen]
        extra_start = name_start + fnlen
        extra = data[extra_start:extra_start + exlen]
        data_start = extra_start + exlen
        raw_compressed = data[data_start:data_start + info.compress_size]
        entries.append({
            "info": info,
            "name_bytes": name,
            "local_extra": extra,
            "raw_compressed": raw_compressed,
            "flag": flag,
            "mtime": mtime,
            "mdate": mdate,
        })
    zf.close()
    return entries


def build_zip(entries, overrides, out_path):
    """
    entries: read_raw_entries() 결과
    overrides: {filename: new_bytes} — 이 항목만 새로 압축, 나머지는 raw 그대로 복사
    """
    out_local = bytearray()
    central_records = []
    offset = 0

    for e in entries:
        info = e["info"]
        name = e["name_bytes"]
        new_content = overrides.get(info.filename)

        flag = e["flag"]
        if new_content is None:
            # 원본 압축 바이트 그대로 재사용 — 압축 알고리즘/레벨 차이로 인한
            # 불필요한 바이트 변형을 원천 차단
            compressed = e["raw_compressed"]
            crc = info.CRC
            usize = info.file_size
            csize = info.compress_size
            method = info.compress_type
        else:
            # ⚠️ 2026-08-30 실증: DEFLATE로 재압축하면 원본의 general-purpose flag가
            # 주장하는 압축강도(Fast 등)와 실제 압축기(Python zlib)가 만든 스트림이
            # 어긋나서 한글이 "손상/변조 가능성"으로 오탐함(내용은 100% 동일해도 재현됨).
            # STORED(무압축)로 쓰면 이 불일치 자체가 성립하지 않아 우회됨 — 실제 한글에서
            # 검증 완료. 텍스트 몇 문단 분량은 무압축이어도 용량 영향 미미.
            usize = len(new_content)
            crc = zlib.crc32(new_content) & 0xFFFFFFFF
            compressed = new_content
            method = zipfile.ZIP_STORED
            flag = 0
            csize = len(compressed)

        local_header = struct.pack(
            LOCAL_HDR_FMT,
            LOCAL_SIG, info.extract_version, flag, method,
            e["mtime"], e["mdate"], crc, csize, usize,
            len(name), len(e["local_extra"]),
        )
        rec_offset = offset
        out_local += local_header + name + e["local_extra"] + compressed
        offset = len(out_local)

        central_records.append({
            "info": info, "name": name, "extra": e["local_extra"],
            "method": method, "crc": crc, "csize": csize, "usize": usize,
            "mtime": e["mtime"], "mdate": e["mdate"], "flag": flag,
            "rec_offset": rec_offset,
        })

    central_start = len(out_local)
    out_central = bytearray()
    for r in central_records:
        info = r["info"]
        version_made_by = (info.create_system << 8) | info.create_version
        central_header = struct.pack(
            "<IHHHHHHIIIHHHHHII",
            CENTRAL_SIG, version_made_by, info.extract_version, r["flag"], r["method"],
            r["mtime"], r["mdate"], r["crc"], r["csize"], r["usize"],
            len(r["name"]), len(r["extra"]), 0, 0,
            info.internal_attr, info.external_attr, r["rec_offset"],
        )
        out_central += central_header + r["name"] + r["extra"]
    central_size = len(out_central)

    eocd = struct.pack(
        "<IHHHHIIH",
        EOCD_SIG, 0, 0, len(central_records), len(central_records),
        central_size, central_start, 0,
    )

    Path(out_path).write_bytes(bytes(out_local) + bytes(out_central) + eocd)


LINESEG_TAG_RE = re.compile(r"<hp:lineseg\b[^>]*?/>")
TEXTPOS_RE = re.compile(r'textpos="(\d+)"')


def trim_lineseg(raw: str, search_from: int, new_len: int) -> tuple[str, int]:
    """new_len 글자로 줄어든 문단의 바로 다음 <hp:linesegarray>에서, 실제 텍스트
    길이를 벗어난 textpos를 가리키는 <hp:lineseg> 항목을 제거한다. 첫 항목은
    텍스트가 비어도 항상 유지. 모듈 docstring 2번 항목 참고 — 이 캐시가 텍스트
    길이보다 앞서가 있으면 한글이 파일 전체를 손상/변조로 판단해 거부함.
    search_from은 방금 바꾼 <hp:t>...</hp:t> 뒤 위치(다음 문단 시작보다 앞이어야 함).
    반환값의 두 번째 값을 다음 호출의 search_from(=cursor)으로 이어서 쓸 것."""
    arr_start = raw.index("<hp:linesegarray>", search_from)
    arr_end = raw.index("</hp:linesegarray>", arr_start)
    inner_start = arr_start + len("<hp:linesegarray>")
    inner = raw[inner_start:arr_end]
    tags = LINESEG_TAG_RE.findall(inner)
    kept = [tag for i, tag in enumerate(tags)
            if i == 0 or int(TEXTPOS_RE.search(tag).group(1)) < new_len]
    new_inner = "".join(kept)
    new_raw = raw[:inner_start] + new_inner + raw[arr_end:]
    new_end = inner_start + len(new_inner) + len("</hp:linesegarray>")
    return new_raw, new_end
