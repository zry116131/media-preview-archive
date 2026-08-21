from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .shot_matcher import normalize_shots


SCRIPT_EXTENSIONS = {".txt", ".md", ".markdown", ".srt", ".docx", ".pdf"}
SHOT_EXTENSIONS = {".json", ".csv"}
MAX_IMPORT_BYTES = 25 * 1024 * 1024


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文本编码，请转换为 UTF-8")


def extract_docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            document = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ValueError("DOCX 文件损坏或格式无效") from exc
    root = ElementTree.fromstring(document)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    lines: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t")).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def extract_pdf(data: bytes, pdftotext: str = "pdftotext") -> str:
    with tempfile.TemporaryDirectory(prefix="framefind-pdf-") as temp_dir:
        source = Path(temp_dir) / "source.pdf"
        source.write_bytes(data)
        try:
            result = subprocess.run(
                [pdftotext, "-layout", str(source), "-"],
                capture_output=True,
                timeout=60,
                check=True,
            )
        except FileNotFoundError as exc:
            raise ValueError("服务器尚未安装 PDF 文本解析组件") from exc
        except subprocess.SubprocessError as exc:
            raise ValueError("PDF 无法读取，可能是扫描件或带有密码") from exc
    text = decode_text(result.stdout).strip()
    if not text:
        raise ValueError("PDF 中没有可提取文字，扫描版 PDF 暂不支持 OCR")
    return text


def clean_srt(text: str) -> str:
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    output = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        lines = [line for line in lines if not line.isdigit() and "-->" not in line]
        if lines:
            output.append(" ".join(lines))
    return "\n".join(output)


def parse_script_file(filename: str, data: bytes, *, pdftotext: str = "pdftotext") -> dict:
    if len(data) > MAX_IMPORT_BYTES:
        raise ValueError("导入文件不能超过 25MB")
    suffix = Path(filename).suffix.casefold()
    if suffix not in SCRIPT_EXTENSIONS:
        raise ValueError("文案支持 TXT、Markdown、SRT、DOCX 和 PDF")
    if suffix == ".docx":
        text = extract_docx(data)
    elif suffix == ".pdf":
        text = extract_pdf(data, pdftotext)
    else:
        text = decode_text(data)
        if suffix == ".srt":
            text = clean_srt(text)
    text = text.strip()
    if not text:
        raise ValueError("文案文件没有可用文字")
    return {
        "title": Path(filename).stem,
        "source_name": filename,
        "source_format": suffix.lstrip("."),
        "source_text": text,
    }


def _row_value(row: dict, *names: str) -> str:
    normalized = {str(key).strip().casefold(): value for key, value in row.items()}
    for name in names:
        value = normalized.get(name.casefold())
        if value not in {None, ""}:
            return str(value).strip()
    return ""


def parse_shot_file(filename: str, data: bytes) -> dict:
    if len(data) > MAX_IMPORT_BYTES:
        raise ValueError("导入文件不能超过 25MB")
    suffix = Path(filename).suffix.casefold()
    if suffix not in SHOT_EXTENSIONS:
        raise ValueError("分镜清单支持 JSON 和 CSV")
    if suffix == ".json":
        try:
            payload = json.loads(decode_text(data))
        except json.JSONDecodeError as exc:
            raise ValueError("JSON 分镜格式无效") from exc
        title = Path(filename).stem
        source_text = ""
        if isinstance(payload, dict):
            title = str(payload.get("title") or title)
            source_text = str(payload.get("source_text") or "")
        shots = normalize_shots(payload)
    else:
        reader = csv.DictReader(io.StringIO(decode_text(data)))
        raw_shots = []
        for row in reader:
            narration = _row_value(row, "文案", "旁白", "narration", "text")
            if not narration:
                continue
            keywords = _row_value(row, "关键词", "keywords")
            raw_shots.append({
                "narration": narration,
                "visual_request": _row_value(row, "画面需求", "visual_request") or narration,
                "shot_type": _row_value(row, "景别", "镜头类型", "shot_type") or "待确认",
                "keywords": [item.strip() for item in re.split(r"[,，、;；]", keywords) if item.strip()],
                "notes": _row_value(row, "备注", "notes"),
            })
        shots = normalize_shots(raw_shots)
        title = Path(filename).stem
        source_text = "\n".join(str(item["narration"]) for item in shots)
    if not shots:
        raise ValueError("分镜文件没有可识别条目")
    return {
        "title": title,
        "source_name": filename,
        "source_format": suffix.lstrip("."),
        "source_text": source_text,
        "shots": shots,
    }


def export_markdown(storyboard: dict, shots: list[dict]) -> bytes:
    lines = [f"# {storyboard['title']}", ""]
    for shot in shots:
        lines += [
            f"## 镜头 {shot['position']:02d}", "",
            f"- 文案：{shot['narration']}",
            f"- 画面需求：{shot['visual_request']}",
            f"- 景别/运镜：{shot['shot_type']}",
            f"- 关键词：{'、'.join(shot.get('keywords', [])) or '—'}",
        ]
        selected = shot.get("selected_candidate")
        if selected:
            lines += [
                f"- 对应素材：{selected.get('filename', '')}",
                f"- SMB 地址：{selected.get('smb_path', '')}",
            ]
        lines.append("")
    return "\n".join(lines).encode("utf-8-sig")


def export_csv(storyboard: dict, shots: list[dict]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["镜号", "文案", "画面需求", "景别/运镜", "关键词", "对应素材", "SMB地址", "备注"])
    for shot in shots:
        selected = shot.get("selected_candidate") or {}
        writer.writerow([
            shot["position"], shot["narration"], shot["visual_request"], shot["shot_type"],
            "、".join(shot.get("keywords", [])), selected.get("filename", ""),
            selected.get("smb_path", ""), shot.get("notes", ""),
        ])
    return output.getvalue().encode("utf-8-sig")


def export_json(storyboard: dict, shots: list[dict]) -> bytes:
    payload = {
        "version": 1,
        "title": storyboard["title"],
        "source_text": storyboard["source_text"],
        "shots": shots,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
