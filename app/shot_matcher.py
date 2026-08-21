from __future__ import annotations

import json
import re
from typing import Iterable

from .paths import windows_unc_path


SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])|\n+")
ASCII_TERM = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9+._-]{1,}")
CHINESE_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")


def fallback_shots(script: str) -> list[dict[str, object]]:
    sentences = [item.strip() for item in SENTENCE_SPLIT.split(script) if item.strip()]
    return [
        {
            "narration": sentence,
            "visual_request": sentence.rstrip("。！？!?；;"),
            "shot_type": "待确认",
            "keywords": [],
        }
        for sentence in sentences[:20]
    ]


def normalize_shots(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        value = value.get("shots", [])
    if not isinstance(value, list):
        return []
    result: list[dict[str, object]] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        narration = str(item.get("narration") or item.get("text") or "").strip()
        if not narration:
            continue
        keywords = item.get("keywords", [])
        if not isinstance(keywords, list):
            keywords = [str(keywords)]
        normalized_keywords: list[str] = []
        for word in keywords:
            normalized_keywords.extend(
                part for part in re.split(r"[,，、;；\s]+", str(word).strip()) if part
            )
        candidates = item.get("candidates", [])
        if not isinstance(candidates, list):
            candidates = []
        selected_asset_id = item.get("selected_asset_id")
        try:
            selected_asset_id = int(selected_asset_id) if selected_asset_id else None
        except (TypeError, ValueError):
            selected_asset_id = None
        result.append({
            "narration": narration,
            "visual_request": str(item.get("visual_request") or narration).strip(),
            "shot_type": str(item.get("shot_type") or "待确认").strip(),
            "keywords": list(dict.fromkeys(normalized_keywords))[:10],
            "notes": str(item.get("notes") or "").strip(),
            "candidates": [candidate for candidate in candidates if isinstance(candidate, dict)][:10],
            "selected_asset_id": selected_asset_id,
        })
    return result


def text_terms(text: str) -> set[str]:
    lowered = text.casefold()
    terms = {match.group(0).casefold() for match in ASCII_TERM.finditer(lowered)}
    for match in CHINESE_RUN.finditer(text):
        run = match.group(0)
        if len(run) <= 4:
            terms.add(run)
        for size in (2, 3):
            terms.update(run[index:index + size] for index in range(len(run) - size + 1))
    return terms


def _json_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = json.loads(str(value or "[]"))
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def _sample_times(value: object) -> list[float]:
    try:
        parsed = json.loads(str(value or "{}"))
        return [float(item) for item in parsed.get("timestamps", [])][:4]
    except (json.JSONDecodeError, TypeError, ValueError):
        return []


def match_shots(
    shots: list[dict[str, object]], assets: Iterable[dict], limit: int = 3
) -> list[dict[str, object]]:
    prepared_assets = []
    for asset in assets:
        keywords = _json_list(asset.get("analysis_keywords"))
        haystack = " ".join([
            str(asset.get("filename", "")),
            str(asset.get("relative_path", "")),
            str(asset.get("analysis_summary", "")),
            " ".join(keywords),
        ]).casefold()
        prepared_assets.append((asset, keywords, haystack, text_terms(haystack)))

    output: list[dict[str, object]] = []
    for index, shot in enumerate(shots):
        keywords = [str(item).casefold() for item in shot.get("keywords", [])]
        request_text = " ".join([
            str(shot.get("narration", "")),
            str(shot.get("visual_request", "")),
            " ".join(keywords),
        ])
        terms = text_terms(request_text)
        ranked = []
        for asset, asset_keywords, haystack, asset_terms in prepared_assets:
            exact = [word for word in keywords if len(word) >= 2 and word in haystack]
            overlap = sorted(terms & asset_terms, key=lambda item: (-len(item), item))
            score = len(exact) * 10 + min(12, len(overlap))
            filename = str(asset.get("filename", "")).casefold()
            score += sum(4 for word in exact if word in filename)
            if score <= 0:
                continue
            reasons = list(dict.fromkeys(exact + overlap))[:5]
            ranked.append((score, asset, asset_keywords, reasons))
        ranked.sort(key=lambda item: (-item[0], item[1]["id"]))

        candidates = []
        for score, asset, asset_keywords, reasons in ranked[:limit]:
            smb_path = windows_unc_path(
                str(asset.get("smb_url", "")), str(asset.get("relative_path", ""))
            )
            candidates.append({
                "asset_id": asset["id"],
                "filename": asset["filename"],
                "relative_path": asset["relative_path"],
                "smb_path": smb_path,
                "media_type": asset["media_type"],
                "duration": asset.get("duration"),
                "summary": asset.get("analysis_summary") or "",
                "keywords": asset_keywords,
                "sample_times": _sample_times(asset.get("analysis_json")),
                "score": score,
                "match_reasons": reasons,
            })
        output.append({"index": index + 1, **shot, "candidates": candidates})
    return output
