# -*- coding: utf-8 -*-
"""
提示词模板管理 API（阶段5第三批）

| 方法 | 路径 | 权限 |
|---|---|---|
| GET  | /api/prompts                                | prompt:read |
| GET  | /api/prompts/{code}                         | prompt:read |
| PUT  | /api/prompts/{code}                         | prompt:write |
| GET  | /api/prompts/{code}/versions                | prompt:read |
| POST | /api/prompts/versions/{version_id}/restore  | prompt:write |

写操作额外要求 CSRF 头；所有写操作写 audit_logs（action=prompt_update）。
路径沿用《阶段5 方案》四接口，增加 {code} 段以同时支持 qa / anomaly 两套模板。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import get_current_user, require_csrf_header, require_permission
from app.db import repo
from app.db.models import User
from app.db.session import session_factory
from app.errors import AppError
from app.services import prompt_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/prompts", tags=["prompts"])

_Reader = Depends(require_permission("prompt:read"))
_Writer = Depends(require_permission("prompt:write"))


class PromptUpdate(BaseModel):
    """编辑请求体：content 必填，change_note 选填（进版本历史，便于回溯）。"""

    content: str = Field(..., description="模板正文（不含代码追加的硬约束）")
    change_note: str = Field("", max_length=200, description="变更说明")


@router.get("")
async def list_prompts(
    user: Annotated[User, Depends(get_current_user)],
    _perm: None = _Reader,
) -> dict:
    """模板列表（qa / anomaly 两套，含当前版本号与字数）。"""
    items = await prompt_service.list_templates()
    return {"total": len(items), "items": items}


@router.get("/{code}")
async def get_current_prompt(
    code: str,
    user: Annotated[User, Depends(get_current_user)],
    _perm: None = _Reader,
) -> dict:
    """当前生效模板（含版本号、变更说明、代码级硬约束提示）。"""
    return await prompt_service.get_current(code)


@router.put("/{code}", dependencies=[Depends(require_csrf_header)])
async def update_prompt(
    code: str,
    body: PromptUpdate,
    user: Annotated[User, Depends(get_current_user)],
    _perm: None = _Writer,
) -> dict:
    """保存编辑：生成新版本并立即生效（在途回答不受影响）。"""
    return await prompt_service.update_current(code, body.content,
                                               body.change_note, user.id)


@router.get("/{code}/versions")
async def list_versions(
    code: str,
    user: Annotated[User, Depends(get_current_user)],
    _perm: None = _Reader,
) -> dict:
    """版本历史（新版本在前，标注当前生效版本）。"""
    return await prompt_service.list_versions(code)


@router.post("/versions/{version_id}/restore", dependencies=[Depends(require_csrf_header)])
async def restore_version(
    version_id: int,
    user: Annotated[User, Depends(get_current_user)],
    _perm: None = _Writer,
) -> dict:
    """恢复历史版本：以旧内容生成新版本号，不覆盖任何历史记录。"""
    async with session_factory() as db:
        async with db.begin():
            ver = await repo.get_prompt_version(db, version_id)
            if ver is None:
                raise AppError("PROMPT_VERSION_NOT_FOUND", "该版本不存在", 404)
            tpl = await repo.get_prompt_template_by_id(db, ver.template_id)
    if tpl is None:
        raise AppError("PROMPT_NOT_FOUND", "模板不存在", 404)
    return await prompt_service.restore_version(tpl.code, version_id, user.id)
