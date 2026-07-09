from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import HTTPException

from services.config import DATA_DIR

LIBRARY_DIR = DATA_DIR / "libraries"
PROMPTS_FILE = LIBRARY_DIR / "prompts.json"
MATERIALS_FILE = LIBRARY_DIR / "materials.json"
MATERIAL_FILES_DIR = LIBRARY_DIR / "material_files"

IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean(value: object, default: str = "") -> str:
    text = str(value if value is not None else default).strip()
    return text or default


def _safe_type(value: object) -> str:
    text = _clean(value, "默认")
    return text[:40] or "默认"


def _safe_name(value: object, default: str = "未命名") -> str:
    text = _clean(value, default)
    return text[:120] or default


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"items": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"items": []}
    return data if isinstance(data, dict) else {"items": []}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _items_from_file(path: Path) -> list[dict[str, Any]]:
    data = _read_json(path)
    items = data.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _extension_for(content_type: str, filename: str = "") -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    if content_type in IMAGE_EXTENSIONS:
        return IMAGE_EXTENSIONS[content_type]
    suffix = Path(filename).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    guessed = mimetypes.guess_extension(content_type) or ".png"
    return ".jpg" if guessed == ".jpe" else guessed


def _assert_image(content_type: str, filename: str = "") -> str:
    normalized = content_type.split(";", 1)[0].strip().lower()
    if normalized.startswith("image/"):
        return normalized
    guessed = mimetypes.guess_type(filename)[0] or ""
    if guessed.startswith("image/"):
        return guessed
    raise HTTPException(status_code=400, detail={"error": "素材必须是图片文件"})


def _safe_material_path(filename: str) -> Path:
    name = Path(filename).name
    if not name or name in {".", ".."}:
        raise HTTPException(status_code=404, detail={"error": "素材文件不存在"})
    path = (MATERIAL_FILES_DIR / name).resolve()
    root = MATERIAL_FILES_DIR.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"error": "素材文件不存在"}) from exc
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail={"error": "素材文件不存在"})
    return path


class PromptLibraryService:
    def __init__(self, path: Path = PROMPTS_FILE):
        self.path = path
        self._lock = Lock()

    def list_items(self) -> dict[str, Any]:
        with self._lock:
            items = sorted(_items_from_file(self.path), key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        types = sorted({str(item.get("type") or "默认") for item in items})
        return {"items": items, "types": types}

    def create_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = _clean(payload.get("content"))
        if not content:
            raise HTTPException(status_code=400, detail={"error": "提示词内容不能为空"})
        now = _now_iso()
        item = {
            "id": uuid.uuid4().hex,
            "name": _safe_name(payload.get("name"), content[:24] or "未命名提示词"),
            "type": _safe_type(payload.get("type")),
            "content": content,
            "note": _clean(payload.get("note"))[:500],
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            items = _items_from_file(self.path)
            items.append(item)
            _write_json(self.path, {"items": items})
        return {"item": item, **self.list_items()}

    def update_item(self, item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            items = _items_from_file(self.path)
            for index, item in enumerate(items):
                if str(item.get("id")) != item_id:
                    continue
                next_item = dict(item)
                if "name" in payload:
                    next_item["name"] = _safe_name(payload.get("name"))
                if "type" in payload:
                    next_item["type"] = _safe_type(payload.get("type"))
                if "content" in payload:
                    content = _clean(payload.get("content"))
                    if not content:
                        raise HTTPException(status_code=400, detail={"error": "提示词内容不能为空"})
                    next_item["content"] = content
                if "note" in payload:
                    next_item["note"] = _clean(payload.get("note"))[:500]
                next_item["updated_at"] = _now_iso()
                items[index] = next_item
                _write_json(self.path, {"items": items})
                return {"item": next_item, **self.list_items()}
        raise HTTPException(status_code=404, detail={"error": "提示词不存在"})

    def delete_item(self, item_id: str) -> dict[str, Any]:
        with self._lock:
            items = _items_from_file(self.path)
            next_items = [item for item in items if str(item.get("id")) != item_id]
            if len(next_items) == len(items):
                raise HTTPException(status_code=404, detail={"error": "提示词不存在"})
            _write_json(self.path, {"items": next_items})
        return self.list_items()


class MaterialLibraryService:
    def __init__(self, path: Path = MATERIALS_FILE):
        self.path = path
        self._lock = Lock()

    def _public_item(self, item: dict[str, Any]) -> dict[str, Any]:
        filename = _clean(item.get("filename"))
        return {
            **item,
            "url": f"/api/material-library/{item.get('id')}/file",
            "thumbnail_url": f"/api/material-library/{item.get('id')}/file",
            "filename": filename,
        }

    def _load_public_items(self) -> list[dict[str, Any]]:
        return [self._public_item(item) for item in _items_from_file(self.path)]

    def list_items(self) -> dict[str, Any]:
        with self._lock:
            items = sorted(self._load_public_items(), key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        types = sorted({str(item.get("type") or "默认") for item in items})
        return {"items": items, "types": types}

    def _find_item(self, item_id: str) -> dict[str, Any]:
        with self._lock:
            for item in _items_from_file(self.path):
                if str(item.get("id")) == item_id:
                    return item
        raise HTTPException(status_code=404, detail={"error": "素材不存在"})

    def file_path(self, item_id: str) -> Path:
        item = self._find_item(item_id)
        return _safe_material_path(str(item.get("filename") or ""))

    def create_from_bytes(
        self,
        *,
        image_data: bytes,
        content_type: str,
        filename: str = "",
        name: str = "",
        type_value: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        if not image_data:
            raise HTTPException(status_code=400, detail={"error": "素材图片不能为空"})
        if len(image_data) > 50 * 1024 * 1024:
            raise HTTPException(status_code=400, detail={"error": "素材图片不能超过 50MB"})
        mime_type = _assert_image(content_type, filename)
        ext = _extension_for(mime_type, filename)
        item_id = uuid.uuid4().hex
        safe_base = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename or "material").stem).strip("._") or "material"
        stored_filename = f"{int(time.time())}_{item_id}_{safe_base[:48]}{ext}"
        MATERIAL_FILES_DIR.mkdir(parents=True, exist_ok=True)
        (MATERIAL_FILES_DIR / stored_filename).write_bytes(image_data)
        now = _now_iso()
        item = {
            "id": item_id,
            "name": _safe_name(name, Path(filename or "素材图片").stem or "素材图片"),
            "type": _safe_type(type_value),
            "note": _clean(note)[:500],
            "filename": stored_filename,
            "mime_type": mime_type,
            "size": len(image_data),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            items = _items_from_file(self.path)
            items.append(item)
            _write_json(self.path, {"items": items})
        return {"item": self._public_item(item), **self.list_items()}

    def create_from_base64(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = _clean(payload.get("b64_json") or payload.get("base64"))
        if raw.startswith("data:"):
            header, _, body = raw.partition(",")
            mime_type = header.split(";", 1)[0].removeprefix("data:") or "image/png"
            raw = body
        else:
            mime_type = _clean(payload.get("mime_type"), "image/png")
        try:
            data = base64.b64decode(raw, validate=True)
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"error": "base64 图片无效"}) from exc
        return self.create_from_bytes(
            image_data=data,
            content_type=mime_type,
            filename=_clean(payload.get("filename"), "material.png"),
            name=_clean(payload.get("name")),
            type_value=_clean(payload.get("type")),
            note=_clean(payload.get("note")),
        )

    def update_item(self, item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            items = _items_from_file(self.path)
            for index, item in enumerate(items):
                if str(item.get("id")) != item_id:
                    continue
                next_item = dict(item)
                if "name" in payload:
                    next_item["name"] = _safe_name(payload.get("name"))
                if "type" in payload:
                    next_item["type"] = _safe_type(payload.get("type"))
                if "note" in payload:
                    next_item["note"] = _clean(payload.get("note"))[:500]
                next_item["updated_at"] = _now_iso()
                items[index] = next_item
                _write_json(self.path, {"items": items})
                return {"item": self._public_item(next_item), **self.list_items()}
        raise HTTPException(status_code=404, detail={"error": "素材不存在"})

    def delete_item(self, item_id: str) -> dict[str, Any]:
        removed: dict[str, Any] | None = None
        with self._lock:
            items = _items_from_file(self.path)
            next_items = []
            for item in items:
                if str(item.get("id")) == item_id:
                    removed = item
                    continue
                next_items.append(item)
            if removed is None:
                raise HTTPException(status_code=404, detail={"error": "素材不存在"})
            _write_json(self.path, {"items": next_items})
        filename = _clean(removed.get("filename"))
        if filename:
            try:
                _safe_material_path(filename).unlink(missing_ok=True)
            except Exception:
                pass
        return self.list_items()


prompt_library_service = PromptLibraryService()
material_library_service = MaterialLibraryService()
