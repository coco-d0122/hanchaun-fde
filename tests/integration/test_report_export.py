# -*- coding: utf-8 -*-
"""
阶段11 集成测试：报告导出（N-13）与指标端点在完整应用栈下的行为。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture
async def seeded_task(admin_client):
    """造一个已完成任务 + 报告草稿（直接落库，绕过 SSE 全链路）。"""
    from uuid import uuid4

    from app.db.models import AgentReportDraft, AgentTask
    from app.db.session import session_factory

    task_id = f"TASK-{uuid4().hex[:12]}"
    draft_id = f"DRAFT-{uuid4().hex[:12]}"
    async with session_factory() as db:
        async with db.begin():
            db.add(AgentTask(
                task_id=task_id, user_id=None, status="completed",
                result_digest={"final_answer": "草稿正文（测试）"}))
            db.add(AgentReportDraft(
                draft_id=draft_id, task_id=task_id, user_id=None,
                content="## 异常现象\n电表 M-002 倍率异常（测试草稿正文）。",
                report_version=1, status="confirmed"))
    return task_id, draft_id


class TestReportExport:
    async def test_export_md(self, user_client, seeded_task):
        task_id, _ = seeded_task
        r = await user_client.get(f"/api/agent/tasks/{task_id}/report?format=md")
        assert r.status_code == 200
        assert "text/markdown" in r.headers["content-type"]
        assert "attachment" in r.headers["content-disposition"]
        body = r.text
        assert "待复核草稿" in body          # 页眉声明
        assert "不代表已确认结论" in body
        assert "倍率异常（测试草稿正文）" in body  # 正文

    async def test_export_html(self, user_client, seeded_task):
        task_id, _ = seeded_task
        r = await user_client.get(f"/api/agent/tasks/{task_id}/report?format=html")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "<!DOCTYPE html>" in r.text
        assert "倍率异常（测试草稿正文）" in r.text

    async def test_export_404_without_draft(self, user_client):
        from uuid import uuid4

        from app.db.models import AgentTask
        from app.db.session import session_factory

        task_id = f"TASK-{uuid4().hex[:12]}"
        async with session_factory() as db:
            async with db.begin():
                db.add(AgentTask(task_id=task_id, user_id=None, status="completed"))
        r = await user_client.get(f"/api/agent/tasks/{task_id}/report")
        assert r.status_code == 404
        assert r.json()["code"] == "REPORT_NOT_FOUND"

    async def test_export_task_missing(self, user_client):
        r = await user_client.get("/api/agent/tasks/TASK-nonexist/report")
        assert r.status_code == 404
