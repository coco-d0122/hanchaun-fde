# -*- coding: utf-8 -*-
"""认证与 RBAC 的 HTTP 层验证：401 / 403 / CSRF，全部走服务端依赖。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio

CSRF = {"X-Requested-With": "XMLHttpRequest"}


async def test_health_is_public_and_reports_retriever(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # 知识库必须真的建好了索引，才算"服务可用"
    assert body["kb"]["chunks"] > 0


async def test_unauthenticated_api_rejected(client):
    for path in ("/api/sessions", "/api/anomalies", "/api/agent/tasks"):
        r = await client.get(path)
        assert r.status_code == 401, f"{path} 未登录应返回 401，实际 {r.status_code}"


async def test_chat_requires_csrf_header(user_client):
    r = await user_client.post("/api/chat", json={"question": "电表倍率变更要核查什么？"})
    assert r.status_code == 403
    assert r.json()["code"] == "CSRF_REJECTED"


async def test_login_failure_message_is_chinese_and_not_enumerable(client):
    r = await client.post("/api/auth/login",
                          json={"username": "pytest_user", "password": "wrong-pass"},
                          headers=CSRF)
    assert r.status_code == 401
    msg = r.json()["message"]
    assert "用户名或密码错误" in msg
    # 不区分"用户不存在"与"密码错误"，避免账号枚举
    r2 = await client.post("/api/auth/login",
                           json={"username": "no-such-user", "password": "wrong-pass"},
                           headers=CSRF)
    assert r2.status_code == 401
    assert r2.json()["message"] == msg


async def test_normal_user_cannot_reach_admin_apis(user_client):
    """普通用户访问管理接口必须 403，且不返回任何数据体。"""
    for method, path in (("GET", "/api/users"), ("GET", "/api/logs"),
                         ("GET", "/api/kb/documents"), ("GET", "/api/prompts")):
        r = await user_client.request(method, path)
        assert r.status_code == 403, f"{path} 应 403，实际 {r.status_code}"
        assert r.json()["code"] == "FORBIDDEN"


async def test_admin_can_reach_admin_apis(admin_client):
    r = await admin_client.get("/api/users")
    assert r.status_code == 200
    assert "items" in r.json() or isinstance(r.json(), list)


async def test_agent_signoff_channel_still_closed(admin_client):
    """
    业务签核接口未开放（405）：任何角色都不能通过 API 写复核结论。
    这是"AI 不能伪造人工审核结果"在协议层的落地。
    """
    r = await admin_client.post("/api/anomalies/AN-20261023-0001/reviews",
                                json={"result": "confirmed"}, headers=CSRF)
    assert r.status_code == 405
