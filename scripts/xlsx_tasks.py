"""Bounded, read-only XLSX text import. Never evaluate formulas or external links."""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

MAX_UPLOAD = 10 * 1024 * 1024
MAX_UNPACKED = 40 * 1024 * 1024
MAX_ROWS = 2000
MAX_COLS = 100
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _xml(archive, name):
    try:
        data = archive.read(name)
    except KeyError as exc:
        raise ValueError(f"XLSX 缺少 {name}") from exc
    if b"\x00" in data:
        raise ValueError("请将工作簿另存为标准 UTF-8 XML 编码的 XLSX")
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("不支持带 DTD 或实体声明的工作簿")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError("工作簿 XML 损坏") from exc


def _text(node):
    return "".join(t.text or "" for t in node.findall(".//s:t", NS))


def _column(reference):
    match = re.fullmatch(r"([A-Z]+)[1-9][0-9]*", reference)
    if not match:
        raise ValueError("单元格地址无效")
    value = 0
    for char in match[1]:
        value = value * 26 + ord(char) - 64
    if value > MAX_COLS:
        raise ValueError(f"任务表最多支持 {MAX_COLS} 列；请移除无关列")
    return value - 1


def read_xlsx(data):
    if not data or len(data) > MAX_UPLOAD:
        raise ValueError("请选择不超过 10 MB 的 XLSX 文件")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("文件不是有效 XLSX；不支持旧版 XLS 或加密工作簿") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(i.file_size for i in entries) > MAX_UNPACKED:
            raise ValueError("工作簿解压后过大；请只保留任务清单")
        if len({i.filename for i in entries}) != len(entries) or any(i.flag_bits & 1 for i in entries):
            raise ValueError("不支持重复 ZIP 条目或加密工作簿")
        names = {i.filename for i in entries}
        if any(n.lower().endswith("vbaproject.bin") for n in names):
            raise ValueError("请另存为不含宏的 XLSX 文件")
        strings = []
        if "xl/sharedStrings.xml" in names:
            strings = [_text(si) for si in _xml(archive, "xl/sharedStrings.xml").findall("s:si", NS)]
        relationships = {}
        for rel in _xml(archive, "xl/_rels/workbook.xml.rels"):
            if rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target", "")
            target = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
            if target.startswith("xl/") and "\\" not in target:
                relationships[rel.get("Id")] = target
        workbook = _xml(archive, "xl/workbook.xml")
        sheets, total_rows = [], 0
        for sheet in workbook.findall("s:sheets/s:sheet", NS):
            target = relationships.get(sheet.get("{" + REL_NS + "}id"))
            if not target or not target.startswith("xl/worksheets/"):
                continue
            rows, width = [], 0
            for row in _xml(archive, target).findall("s:sheetData/s:row", NS):
                cells, errors = {}, []
                for cell in row.findall("s:c", NS):
                    idx = _column(cell.get("r", ""))
                    if idx in cells:
                        raise ValueError("同一行存在重复单元格地址")
                    kind = cell.get("t")
                    value = cell.findtext("s:v", default="", namespaces=NS)
                    if cell.find("s:f", NS) is not None:
                        value = ""
                        errors.append({"column": idx, "message": "公式单元格请先复制并粘贴为值"})
                    elif kind == "s":
                        try:
                            number = int(value)
                            if number < 0:
                                raise ValueError()
                            value = strings[number]
                        except (ValueError, IndexError) as exc:
                            raise ValueError("共享字符串索引无效") from exc
                    elif kind == "inlineStr":
                        value = _text(cell)
                    elif kind == "e":
                        errors.append({"column": idx, "message": "单元格包含 Excel 错误值"})
                    if len(value) > 100000:
                        raise ValueError("单个单元格最多支持 100000 字符")
                    cells[idx] = value
                if not any(v.strip() for v in cells.values()) and not errors:
                    continue
                width = max(width, max(cells, default=0) + 1)
                try:
                    row_number = int(row.get("r", str(len(rows) + 1)))
                except ValueError as exc:
                    raise ValueError("工作表行号无效") from exc
                rows.append({"row_number": row_number, "cells": cells, "errors": errors})
                total_rows += 1
                if total_rows > MAX_ROWS + 30:
                    raise ValueError(f"工作簿最多支持 {MAX_ROWS} 条任务；请拆分后导入")
            if not rows:
                continue
            header = rows.pop(0)
            if header["errors"]:
                raise ValueError("表头不能包含公式或错误值")
            headers = [header["cells"].get(i, "").strip() or f"未命名列 {i + 1}" for i in range(width)]
            sheets.append({"name": sheet.get("name", "工作表"), "headers": headers,
                           "rows": [{**r, "cells": [r["cells"].get(i, "") for i in range(width)]} for r in rows],
                           "hidden": sheet.get("state", "visible") != "visible"})
        if not sheets:
            raise ValueError("未找到可读取的工作表；首个非空行应为表头")
        if sum(len(sheet["rows"]) for sheet in sheets) > MAX_ROWS:
            raise ValueError(f"工作簿最多支持 {MAX_ROWS} 条任务；请拆分后导入")
        if len(sheets) > 30:
            raise ValueError("最多支持 30 个非空工作表")
        return {"sheets": sheets, "note": "首个非空行作为表头；读取文本值，不执行公式、宏或外部链接。"}
