# -*- coding: utf-8 -*-
"""知识库管理 API（阶段5第二批）。全部需登录 + 对应权限码 + CSRF 头（写操作）。"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from app.api.deps import get_current_user, require_csrf_header, require_permission
from app.core.config import get_settings
from app.db import repo
from app.db.models import User
from app.db.session import session_factory
from app.errors import AppError
from app.services import kb_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/kb", tags=["knowledge-base"])

# 读取类需要的权限（admin 有 kb:read；普通用户无——知识库维护是管理职能）
_Reader = Depends(require_permission("kb:read"))
_Writer = Depends(require_permission("kb:write"))
_Deleter = Depends(require_permission("kb:delete"))


@router.get("/documents")
async def list_documents(
    user: Annotated[User, Depends(get_current_user)],
    _perm: None = _Reader,
) -> dict:
    """文档管理列表（名称/类型/版本/状态/切片数/上传人/上传时间）。"""
    async with session_factory() as db:
        async with db.begin():
            items = await repo.list_kb_documents(db)
    return {"total": len(items), "items": items}


@router.get("/documents/{doc_id}")
async def get_document(
    doc_id: int,
    user: Annotated[User, Depends(get_current_user)],
    _perm: None = _Reader,
) -> dict:
    """文档详情（基础信息 + 版本历史 + 错误说明）。"""
    async with session_factory() as db:
        async with db.begin():
            detail = await repo.get_kb_document_detail(db, doc_id)
    if detail is None:
        raise AppError("KB_DOC_NOT_FOUND", "知识库文档不存在", 404)
    return detail


@router.post("/documents", dependencies=[Depends(require_csrf_header)])
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    doc_type: str = Form("other"),
    user: Annotated[User, Depends(get_current_user)] = None,   # noqa: RUF013
    _perm: None = _Writer,
) -> dict:
    """
    上传文档（multipart：file + doc_type）。

    成功 → 发布并立即对新问答生效；失败 → 中文错误说明（4xx），
    线上检索不受任何影响。
    """
    cfg = get_settings()
    file_bytes = await file.read()
    if not file_bytes:
        raise AppError("KB_EMPTY_FILE", "上传文件为空", 400)

    doc = await kb_service.process_upload(
        request.app, cfg, file_bytes, file.filename or "未命名.md",
        doc_type, user.id if user else 0)
    return {
        "ok": True,
        "doc_id": doc.id,
        "filename": doc.filename,
        "status": doc.status,
        "message": "文档已发布，新发起的问答立即生效",
    }


@router.delete("/documents/{doc_id}", dependencies=[Depends(require_csrf_header)])
async def delete_document(
    doc_id: int,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    _perm: None = _Deleter,
) -> dict:
    """软删除文档：检索内容立即移除，行保留供审计。内建文档不可删。"""
    cfg = get_settings()
    await kb_service.delete_document(request.app, cfg, doc_id, user.id)
    return {"ok": True, "message": "文档已删除，检索内容已同步移除"}


@router.post("/documents/{doc_id}/reprocess", dependencies=[Depends(require_csrf_header)])
async def reprocess_document(
    doc_id: int,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    _perm: None = _Writer,
) -> dict:
    """重新处理文档（failed / needs_ocr / published 均可触发；内容未变时幂等秒级）。"""
    cfg = get_settings()
    await kb_service.reprocess_document(request.app, cfg, doc_id, user.id)
    return {"ok": True, "message": "文档已重新处理并发布"}
