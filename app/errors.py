# -*- coding: utf-8 -*-
"""
统一错误处理（阶段4方案第八节）

结构：{ "code": 业务错误码, "message": 中文错误信息, "details": 可选详情 }

边界：
  - 建流之前（参数校验、会话/事件查询、KB 状态检查）→ HTTP 状态码 + JSON 错误体
  - 建流之后（检索失败、LLM 超时/断连、生成中途异常）→ SSE error 事件 + 同结构 JSON
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """业务错误基类：携带错误码、HTTP 状态码、中文信息与可选详情。"""

    def __init__(self, code: str, message: str, http_status: int = 400, details: Any = None):
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details
        super().__init__(message)

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


# ---- 具体错误码（与方案 8.1 表一一对应）----

def param_invalid(details: Any = None) -> AppError:
    return AppError("PARAM_INVALID", "请求参数不合法：问题不能为空，且不超过 500 字", 422, details)


def anomaly_not_specified() -> AppError:
    return AppError("ANOMALY_NOT_SPECIFIED",
                    "异常分析模式需要指定事件：请在问题中指明电表编号，或在异常面板中选择事件", 400)


def session_not_found(session_id: str) -> AppError:
    return AppError("SESSION_NOT_FOUND", "会话不存在或已过期", 404, {"session_id": session_id})


def anomaly_not_found(anomaly_id: str) -> AppError:
    return AppError("ANOMALY_NOT_FOUND", "异常事件不存在或已被删除", 404, {"anomaly_id": anomaly_id})


def kb_not_ready() -> AppError:
    return AppError("KB_NOT_READY", "知识库尚未初始化完成，请稍后重试或联系管理员", 503)


def db_error(details: Any = None) -> AppError:
    return AppError("DB_ERROR", "数据读写异常，请稍后重试", 500, details)


def retrieve_failed(details: Any = None) -> AppError:
    return AppError("RETRIEVE_FAILED", "知识检索失败，请重试", 500, details)


def llm_unavailable(details: Any = None) -> AppError:
    return AppError("LLM_UNAVAILABLE", "模型服务当前不可用", 503, details)


def timeout_error() -> AppError:
    return AppError("TIMEOUT", "模型响应超时，请重试", 504)


# ---- 注册到 FastAPI ----

def register_error_handlers(app: FastAPI) -> None:
    """把统一 JSON 错误结构挂到应用上（含 Pydantic 校验失败的翻译）。"""

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI 默认 422 返回英文 detail，这里翻译为统一结构
        return JSONResponse(
            status_code=422,
            content={"code": "PARAM_INVALID",
                     "message": "请求参数不合法，请检查字段类型与取值范围",
                     "details": [{"field": ".".join(str(x) for x in e.get("loc", [])),
                                  "msg": e.get("msg", "")} for e in exc.errors()[:5]]},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # 兜底：404 路由等也走统一结构（中文信息）
        msg = {404: "请求的资源不存在", 405: "请求方法不被允许"}.get(exc.status_code, "服务器内部错误")
        return JSONResponse(status_code=exc.status_code,
                             content={"code": f"HTTP_{exc.status_code}", "message": msg,
                                      "details": None})

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        return JSONResponse(status_code=500,
                             content={"code": "INTERNAL_ERROR",
                                      "message": "服务器内部错误，请稍后重试",
                                      "details": type(exc).__name__})


class AgentToolError(Exception):
    """
    工具层错误：code 进错误结构，message 是给模型/用户看的中文说明。

    放在 errors.py 而不是 tools.py，是因为 energy/ 下的领域模块也要抛它；
    若定义在 agent 层，energy 就得反向依赖 agent，形成循环导入。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
