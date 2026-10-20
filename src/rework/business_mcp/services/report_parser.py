"""완성 보고서 PDF를 표·제안·각주로 구조화한다.

PDF의 표는 테두리가 머리글에만 있어서, 머리글 칸의 x 범위를 기준으로
아래쪽 단어를 열에 배정해 행을 복원한다.
"""

import logging
import re
from pathlib import Path

import pdfplumber

logging.getLogger("pdfminer").setLevel(logging.ERROR)

SECTION_RE = re.compile(r"^(\d+)\.\s*(.+)$")
MARKERS = "①②③④⑤⑥⑦⑧⑨⑩"
ROW_GAP = 26  # 이보다 큰 세로 간격이면 표가 끝난 것으로 본다


class ReportError(ValueError):
    pass


def _lines(words: list[dict], tol: float = 3) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _text(line: list[dict]) -> str:
    return " ".join(w["text"] for w in line)


def _page_tables(page, page_no: int) -> list[dict]:
    words = page.extract_words(x_tolerance=1.5, keep_blank_chars=False)
    lines = _lines(words)
    headers = []
    for t in page.find_tables():
        head = t.rows[0]
        cells = [c for c in head.cells if c is not None]
        names = [(page.crop(c).extract_text() or "").strip() for c in cells]
        if not any(names) or "결" in names[0]:
            continue  # 결재란·그림 요소
        headers.append({"bbox": t.bbox, "cells": cells, "columns": names})
    headers.sort(key=lambda h: h["bbox"][1])

    tables = []
    for i, h in enumerate(headers):
        x0, x1 = h["cells"][0][0], h["cells"][-1][2]
        bottom = h["bbox"][3]
        limit = headers[i + 1]["bbox"][1] if i + 1 < len(headers) else page.height
        # 제목: 머리글 위쪽에서 가장 가까운 "N. 제목" 줄
        title, section_no = "", None
        for line in lines:
            if line[0]["bottom"] <= h["bbox"][1] + 1:
                m = SECTION_RE.match(_text(line))
                if m:
                    section_no, title = int(m.group(1)), m.group(2)
        # 행 구분선: 표 폭의 절반 이상을 덮는 가로선
        width = x1 - x0
        cover: dict[float, float] = {}
        for e in page.edges:
            if e["orientation"] == "h" and bottom + 2 < e["top"] < limit and e["x1"] > x0 and e["x0"] < x1:
                key = next((k for k in cover if abs(k - e["top"]) < 2), e["top"])
                cover[key] = cover.get(key, 0) + min(e["x1"], x1) - max(e["x0"], x0)
        seps = sorted(k for k, v in cover.items() if v > width * 0.5)

        rows: list[list[str]] = []
        notes: list[str] = []
        row_cells: list[str] = []
        row_idx = -1
        last_bottom = bottom
        for line in lines:
            if line[0]["top"] < bottom - 1 or line[0]["top"] >= limit:
                continue
            inside = [w for w in line if x0 - 2 <= (w["x0"] + w["x1"]) / 2 <= x1 + 2]
            if not inside:
                continue
            txt = _text(inside)
            if txt.startswith("※"):
                notes.append(txt)
                break
            if SECTION_RE.match(txt) or line[0]["top"] - last_bottom > ROW_GAP:
                break
            last_bottom = line[0]["bottom"]
            # 몇 번째 구분선 위에 있는지로 행을 정한다. 구분선이 없으면 줄마다 한 행.
            idx = sum(1 for s in seps if s < line[0]["top"]) if seps else len(rows)
            if seps and idx >= len(seps):
                break
            if idx != row_idx:
                row_cells = [""] * len(h["cells"])
                rows.append(row_cells)
                row_idx = idx
            for w in inside:
                cx = (w["x0"] + w["x1"]) / 2
                for ci, c in enumerate(h["cells"]):
                    if c[0] - 2 <= cx <= c[2] + 2:
                        row_cells[ci] = (row_cells[ci] + " " + w["text"]).strip()
                        break
        tables.append({
            "table_id": f"p{page_no}t{i + 1}", "page": page_no, "section_no": section_no,
            "title": title, "columns": h["columns"], "rows": rows, "notes": notes,
        })
    return tables


def parse_number(raw: str) -> float | None:
    s = raw.replace(",", "").replace("−", "-").replace("–", "-").replace("%", "").replace("+", "").strip()
    s = re.sub(r"[일개건원천]+$", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def parse_report(path: str | Path) -> dict:
    try:
        pdf = pdfplumber.open(str(path))
    except Exception as e:  # noqa: BLE001
        raise ReportError(f"PDF를 열 수 없습니다: {e}") from e
    with pdf:
        full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        tables = [t for i, p in enumerate(pdf.pages, 1) for t in _page_tables(p, i)]

    m = re.search(r"(\d{4})년\s*(\d{1,2})월", full_text)
    if not m:
        raise ReportError("보고서 제목에서 연·월을 찾지 못했습니다.")
    year, month = int(m.group(1)), int(m.group(2))
    prev_year, prev_month = (year, month - 1) if month > 1 else (year - 1, 12)
    periods = {f"{month}월": f"{year}-{month:02d}", f"{prev_month}월": f"{prev_year}-{prev_month:02d}"}

    sections: list[dict] = []
    for line in full_text.splitlines():
        sm = SECTION_RE.match(line.strip())
        if sm and len(sm.group(2)) < 40:
            sections.append({"no": int(sm.group(1)), "title": sm.group(2), "text": ""})
        elif sections:
            sections[-1]["text"] += line.strip() + "\n"

    proposals = []
    data_tables = []
    for t in tables:
        if "제안" in t["columns"][0] or "제안" in t["title"]:
            for r in t["rows"]:
                label = r[0]
                key = f"P{len(proposals) + 1}"
                proposals.append({"key": key, "title": label.lstrip(MARKERS).strip(), "text": " ".join(r[1:]).strip()})
        else:
            data_tables.append(t)
    if not proposals:
        raise ReportError("보고서에서 제안 항목을 찾지 못했습니다.")

    notes = [ln.strip() for ln in full_text.splitlines() if ln.strip().startswith("※")]
    return {
        "title": full_text.splitlines()[0] if full_text else "",
        "report_title": m.group(0) + " 보고서",
        "year": year, "month": month,
        "period": periods[f"{month}월"], "prev_period": periods[f"{prev_month}월"],
        "periods": periods,
        "sections": [{"no": s["no"], "title": s["title"], "text": s["text"].strip()[:1500]} for s in sections],
        "tables": data_tables,
        "proposals": proposals,
        "notes": notes,
    }
