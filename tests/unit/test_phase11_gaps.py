# -*- coding: utf-8 -*-
"""
阶段11 单元测试：Token 日预算 / 监控指标 / 行级数据范围 / 幂等采集。
"""

from __future__ import annotations

import pytest

from tests.conftest import CSRF_HEADERS

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _tables():
    """本文件部分用例不经 client fixture（不触发 lifespan），先确保建表。

    用独立事件循环执行（sync fixture 对 async 测试同样生效）；
    create_all 幂等，与 client fixture 的建表不冲突。
    """
    import asyncio

    from app.db.models import Base
    from app.db.session import engine

    async def _create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    yield

# ============================== Token 日预算（N-05） ==============================


class TestTokenBudget:
    def test_estimate_tokens_cjk_and_ascii(self):
        from app.core.token_budget import estimate_tokens

        assert estimate_tokens("") == 0
        # 纯中文：1 字 1 token
        assert estimate_tokens("电表倍率异常" * 10) == 60
        # 纯 ASCII：约 4 字符 1 token
        assert estimate_tokens("a" * 40) == 10
        # 混合
        assert estimate_tokens("电表 M-002 异常") > 0

    async def test_record_and_sum(self):
        from app.core.token_budget import get_daily_usage, record_llm_usage, utc_today
        from app.db.session import session_factory

        await record_llm_usage(None, "chat", "你好，电表", "回答内容")
        await record_llm_usage(None, "agent", "", "", prompt_tokens=100,
                               completion_tokens=50)
        async with session_factory() as db:
            async with db.begin():
                used = await get_daily_usage(db, None, utc_today())
        # 显式 tokens 优先（100+50），文本估算 > 0
        assert used > 150

    async def test_enforce_budget_blocks_when_exhausted(self):
        """日累计超预算 → 429 中文提示；预算 0 = 不限。"""
        from types import SimpleNamespace

        from app.core import token_budget

        # 先记账一笔超过预算的用量
        await token_budget.record_llm_usage(424242, "chat", "", "",
                                            prompt_tokens=10, completion_tokens=10)
        cfg = SimpleNamespace(llm_daily_token_budget=5)
        with pytest.raises(Exception) as exc:
            await token_budget.enforce_daily_budget(424242, cfg)
        assert getattr(exc.value, "http_status", None) == 429
        assert "额度已用完" in getattr(exc.value, "message", "")

        # 预算 0 → 不限制
        cfg0 = SimpleNamespace(llm_daily_token_budget=0)
        assert await token_budget.enforce_daily_budget(424242, cfg0) == 0

    async def test_chat_budget_endpoint_429(self, user_client):
        """把预算压小并预注满用量 → POST /api/chat 返回 429。"""
        from sqlalchemy import delete

        from app.core import token_budget
        from app.core.config import get_settings
        from app.db import repo
        from app.db.models import LlmUsage
        from app.db.session import session_factory

        async with session_factory() as db:
            async with db.begin():
                user = await repo.get_user_by_name(db, "pytest_user")
        uid = user.id

        async with session_factory() as db:
            async with db.begin():
                db.add(LlmUsage(user_id=uid, kind="chat", prompt_tokens=10**9,
                                completion_tokens=0, day=token_budget.utc_today()))

        # 直接把配置预算临时改小（内存对象，不影响其他测试的独立进程）
        cfg = get_settings()
        old = cfg.llm_daily_token_budget
        cfg.llm_daily_token_budget = 1000
        try:
            r = await user_client.post(
                "/api/chat", headers=CSRF_HEADERS,
                json={"question": "配电室巡检要求是什么？", "mode": "knowledge"})
            assert r.status_code == 429
            body = r.json()
            assert body["code"] == "LLM_BUDGET_EXCEEDED"
            assert "额度已用完" in body["message"]
        finally:
            cfg.llm_daily_token_budget = old
            # 清理：删掉注入的巨额用量行，避免污染同进程的其他测试
            async with session_factory() as db:
                async with db.begin():
                    await db.execute(delete(LlmUsage).where(
                        LlmUsage.prompt_tokens >= 10**9))


# ============================== 监控指标（N-06） ==============================


class TestMetrics:
    async def test_metrics_endpoint_admin_only(self, client, user_client):
        from tests.conftest import _make_user

        # 普通用户 → 403（user_client 与 client 是同一对象，此刻 Cookie 是普通用户）
        r = await user_client.get("/api/metrics")
        assert r.status_code == 403

        # 清除登录态 → 401
        client.cookies.clear()
        r = await client.get("/api/metrics")
        assert r.status_code == 401

        # 管理员 → 200 且含三组指标
        await _make_user("pytest_admin", role="admin")
        r = await client.post("/api/auth/login",
                              json={"username": "pytest_admin",
                                    "password": "TestPass12345"},
                              headers=CSRF_HEADERS)
        assert r.status_code == 200
        r = await client.get("/api/metrics")
        assert r.status_code == 200
        text = r.text
        assert "http_requests_total" in text
        assert "agent_tool_failure_ratio" in text
        assert "llm_tokens_used_today" in text

    async def test_metrics_counts_requests(self, admin_client):
        from app.core.metrics import METRICS

        before = sum(METRICS.requests_total.values())
        await admin_client.get("/api/health")
        assert sum(METRICS.requests_total.values()) >= before + 1

    def test_bucket(self):
        from app.core.metrics import _bucket

        assert _bucket("/api/chat") == "/api/chat"
        assert _bucket("/api/agent/tasks/TASK-x/report") == "/api/agent"
        assert _bucket("/admin/dashboard") == "/admin"
        assert _bucket("/favicon.ico") == "other"


# ============================== 行级数据范围（N-02） ==============================


class TestBuildingScope:
    async def _set_scope(self, user_id: int, scope_type: str, values: list[str]):
        from app.db import repo
        from app.db.session import session_factory

        async with session_factory() as db:
            async with db.begin():
                await repo.set_user_scope(db, user_id, scope_type, values)

    async def _get_user_id(self, username: str) -> int:
        from app.db import repo
        from app.db.session import session_factory

        async with session_factory() as db:
            async with db.begin():
                user = await repo.get_user_by_name(db, username)
        return user.id

    async def test_scope_filters_list_and_blocks_detail(self, user_client):
        # 先看全量列表，取一个真实楼栋
        r = await user_client.get("/api/anomalies")
        assert r.status_code == 200
        items = r.json()["items"]
        assert items, "种子异常事件应为空是不正常的"
        outside = items[0]["building_id"]

        uid = await self._get_user_id("pytest_user")
        # 拿到另一个不同的楼栋作为授权范围（若只有一个楼栋则用空列表+403 断言跳过）
        buildings = sorted({i["building_id"] for i in items})
        if len(buildings) < 2:
            pytest.skip("种子数据只有一个楼栋，无法构造范围外楼栋")
        allowed = [b for b in buildings if b != outside][0]
        await self._set_scope(uid, "buildings", [allowed])

        r = await user_client.get("/api/anomalies")
        body = r.json()
        assert all(i["building_id"] == allowed for i in body["items"])
        assert body["scope"]["type"] == "buildings"

        # 范围外详情 → 403
        target = items[0]["anomaly_id"]
        r = await user_client.get(f"/api/anomalies/{target}")
        assert r.status_code == 403
        assert r.json()["code"] == "FORBIDDEN"

        # 显式请求范围外楼栋 → 403
        r = await user_client.get(f"/api/anomalies?building_id={outside}")
        assert r.status_code == 403

        # 恢复 all
        await self._set_scope(uid, "all", [])
        r = await user_client.get(f"/api/anomalies/{target}")
        assert r.status_code == 200

    async def test_admin_scope_endpoint(self, admin_client):
        from tests.conftest import _make_user

        # 目标用户：直接落库创建（不与 admin 共用客户端 Cookie）
        await _make_user("pytest_scope_user", role="user")
        r = await admin_client.get("/api/users")
        assert r.status_code == 200, r.text
        uid = next(u["id"] for u in r.json()["items"]
                   if u["username"] == "pytest_scope_user")

        # admin 自身不允许设 scope
        admin_id = next(u["id"] for u in r.json()["items"]
                        if u["username"] == "pytest_admin")
        r = await admin_client.put(f"/api/users/{admin_id}/scope", headers=CSRF_HEADERS,
                                   json={"scope_type": "buildings", "scope_values": ["B1"]})
        assert r.status_code == 400

        # 给普通用户设范围
        r = await admin_client.put(f"/api/users/{uid}/scope", headers=CSRF_HEADERS,
                                   json={"scope_type": "buildings", "scope_values": ["B1"]})
        assert r.status_code == 200, r.text
        r = await admin_client.get(f"/api/users/{uid}/scope")
        assert r.json() == {"user_id": uid, "scope_type": "buildings", "buildings": ["B1"]}

        # 空 buildings 被拒
        r = await admin_client.put(f"/api/users/{uid}/scope", headers=CSRF_HEADERS,
                                   json={"scope_type": "buildings", "scope_values": []})
        assert r.status_code == 422

        # 恢复
        r = await admin_client.put(f"/api/users/{uid}/scope", headers=CSRF_HEADERS,
                                   json={"scope_type": "all", "scope_values": []})
        assert r.status_code == 200

    async def test_admin_ignores_scope(self, admin_client):
        """admin 角色恒为 all：即使误配 scope_values 也不受限制。"""
        from app.db import repo
        from app.db.session import session_factory

        async with session_factory() as db:
            async with db.begin():
                from sqlalchemy import select

                from app.db.models import User
                admin = (await db.execute(
                    select(User).where(User.username == "pytest_admin"))).scalar_one()
                await repo.set_user_scope(db, admin.id, "buildings", ["NOWHERE"])
                scope = await repo.get_user_building_scope(db, admin.id)
        assert scope == ("all", [])


# ============================== 幂等采集（N-01 落库通道 / D2-05） ==============================


class TestMeterReadingIngest:
    async def test_unique_key_idempotent(self):
        """唯一键 (meter_id, ts)：同批次重复导入不翻倍。"""
        from datetime import datetime

        from sqlalchemy import func, select

        from app.db.models import MeterReadingRow
        from app.db.session import session_factory

        row = dict(meter_id="M-TEST-1", ts=datetime(2026, 1, 1, 0, 0),
                   raw_reading=100.0, multiplier=1.0, unit="kWh",
                   building_id="B1", parent_meter_id=None)
        async with session_factory() as db:
            async with db.begin():
                db.add(MeterReadingRow(**row))

        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            async with session_factory() as db:
                async with db.begin():
                    db.add(MeterReadingRow(**row))
        # 唯一键命中：仍只有 1 行
        async with session_factory() as db:
            async with db.begin():
                n = (await db.execute(
                    select(func.count(MeterReadingRow.id))
                    .where(MeterReadingRow.meter_id == "M-TEST-1"))).scalar_one()
        assert n == 1

    async def test_ingest_script_idempotent(self, tmp_path):
        """ingest_meter_batch 同一 CSV 跑两遍：第二遍 inserted=0。"""
        from scripts import ingest_meter_batch as ingest_mod

        csv_path = tmp_path / "batch.csv"
        csv_path.write_text(
            "meter_id,timestamp,raw_reading,multiplier,unit,building_id,parent_meter_id\n"
            "M-TEST-9,2026-01-01 00:00,100,10,kWh,B1,\n"
            "M-TEST-9,2026-01-01 00:15,102,10,kWh,B1,\n"
            "M-TEST-9,2026-01-01 00:30,105,10,kWh,B1,\n",
            encoding="utf-8")
        first = await ingest_mod.ingest(csv_path)
        second = await ingest_mod.ingest(csv_path)
        assert first["inserted"] == 3
        assert second["inserted"] == 0
        assert second["duplicated_skipped"] == 3
        assert first["table_total"] == second["table_total"]
