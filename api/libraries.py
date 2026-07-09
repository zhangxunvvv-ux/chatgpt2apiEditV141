from __future__ import annotations

from fastapi import APIRouter, File, Form, Header, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api.support import require_identity
from services.library_service import material_library_service, prompt_library_service


class PromptPayload(BaseModel):
    name: str = ""
    type: str = "默认"
    content: str = ""
    note: str = ""


class MaterialPayload(BaseModel):
    name: str = ""
    type: str = "默认"
    note: str = ""
    b64_json: str = ""
    base64: str = ""
    mime_type: str = "image/png"
    filename: str = "material.png"


class MaterialUpdatePayload(BaseModel):
    name: str | None = None
    type: str | None = None
    note: str | None = None


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/prompt-library")
    async def list_prompts(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        return await run_in_threadpool(prompt_library_service.list_items)

    @router.post("/api/prompt-library")
    async def create_prompt(body: PromptPayload, authorization: str | None = Header(default=None)):
        require_identity(authorization)
        return await run_in_threadpool(prompt_library_service.create_item, body.model_dump())

    @router.post("/api/prompt-library/{item_id}")
    async def update_prompt(item_id: str, body: PromptPayload, authorization: str | None = Header(default=None)):
        require_identity(authorization)
        return await run_in_threadpool(prompt_library_service.update_item, item_id, body.model_dump())

    @router.delete("/api/prompt-library/{item_id}")
    async def delete_prompt(item_id: str, authorization: str | None = Header(default=None)):
        require_identity(authorization)
        return await run_in_threadpool(prompt_library_service.delete_item, item_id)

    @router.get("/api/material-library")
    async def list_materials(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        return await run_in_threadpool(material_library_service.list_items)

    @router.post("/api/material-library/upload")
    async def upload_material(
        file: UploadFile = File(...),
        name: str = Form(default=""),
        type: str = Form(default="默认"),
        note: str = Form(default=""),
        authorization: str | None = Header(default=None),
    ):
        require_identity(authorization)
        image_data = await file.read()
        try:
            return await run_in_threadpool(
                material_library_service.create_from_bytes,
                image_data=image_data,
                content_type=file.content_type or "application/octet-stream",
                filename=file.filename or "material.png",
                name=name,
                type_value=type,
                note=note,
            )
        finally:
            await file.close()

    @router.post("/api/material-library/from-base64")
    async def create_material_from_base64(body: MaterialPayload, authorization: str | None = Header(default=None)):
        require_identity(authorization)
        return await run_in_threadpool(material_library_service.create_from_base64, body.model_dump())

    @router.post("/api/material-library/{item_id}")
    async def update_material(item_id: str, body: MaterialUpdatePayload, authorization: str | None = Header(default=None)):
        require_identity(authorization)
        return await run_in_threadpool(
            material_library_service.update_item,
            item_id,
            body.model_dump(exclude_unset=True),
        )

    @router.delete("/api/material-library/{item_id}")
    async def delete_material(item_id: str, authorization: str | None = Header(default=None)):
        require_identity(authorization)
        return await run_in_threadpool(material_library_service.delete_item, item_id)

    @router.get("/api/material-library/{item_id}/file")
    async def get_material_file(item_id: str):
        path = await run_in_threadpool(material_library_service.file_path, item_id)
        return FileResponse(path)

    return router
