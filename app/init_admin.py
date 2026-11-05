# -*- coding: utf-8 -*-
"""
初始管理员建号 CLI（阶段5 D5：交互式建号，不硬编码任何默认密码）

用法：
    python -m app.init_admin                        # 交互式
    python -m app.init_admin --username admin --role admin   # 半自动（密码仍交互输入）
    python -m app.init_admin --list                 # 查看现有用户

幂等：用户名已存在时提示并退出，不覆盖已有账号。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from app.core.security import hash_password
from app.db import repo
from app.db.models import Base
from app.db.session import engine, session_factory


def _read_password() -> str:
    """
    读取密码：优先交互式 getpass（不回显）；非 TTY 环境（管道/CI）时
    读取环境变量 INIT_PASSWORD（不落命令行参数——命令行会被 shell history 记录）。
    """
    if sys.stdin is not None and sys.stdin.isatty():
        while True:
            pw = getpass.getpass("设置密码（≥8 位）: ")
            if len(pw) < 8:
                print("✘ 密码至少 8 位，请重试")
                continue
            pw2 = getpass.getpass("再输入一次确认: ")
            if pw != pw2:
                print("✘ 两次输入不一致，请重试")
                continue
            return pw
    env_pw = os.getenv("INIT_PASSWORD", "")
    if len(env_pw) >= 8:
        return env_pw
    print("✘ 非交互环境请通过环境变量 INIT_PASSWORD 提供密码（≥8 位）")
    sys.exit(1)


async def _list_users() -> None:
    async with session_factory() as db:
        async with db.begin():
            items = await repo.list_users(db)
    if not items:
        print("（暂无用户）")
    for u in items:
        roles = "/".join(r["code"] for r in u["roles"]) or "-"
        state = "启用" if u["is_active"] else "禁用"
        print(f"  #{u['id']:<3} {u['username']:<16} 角色 {roles:<8} {state}")


async def _create_admin(username: str, role: str) -> int:
    # 建表（幂等，保证 CLI 可在全新环境直接跑）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_factory() as db:
        async with db.begin():
            await repo.seed_roles_permissions(db)
            existing = await repo.get_user_by_name(db, username)
            if existing is not None:
                print(f"✘ 用户名 {username!r} 已存在（未做任何修改）")
                return 1

    pw = _read_password()

    async with session_factory() as db:
        async with db.begin():
            user = await repo.create_user(db, username, hash_password(pw), role)
            await repo.write_audit_log(
                db, "user_create", user.id,
                object_type="user", object_id=str(user.id),
                detail={"username": username, "role": role, "via": "cli"})
    print(f"✔ 用户已创建：#{user.id} {username}（角色 {role}）")
    print("  密码以 bcrypt 哈希存库（$2b$ 前缀），明文未落任何文件。")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="瀚川系统 · 初始建号 CLI")
    parser.add_argument("--username", default=None, help="用户名（缺省交互输入）")
    parser.add_argument("--role", default="admin", choices=["user", "admin"])
    parser.add_argument("--list", action="store_true", help="仅列出现有用户")
    args = parser.parse_args()

    if args.list:
        asyncio.run(_list_users())
        return

    username = args.username
    if not username:
        username = input("用户名: ").strip()
        if not username:
            print("✘ 用户名不能为空")
            sys.exit(1)

    sys.exit(asyncio.run(_create_admin(username, args.role)))


if __name__ == "__main__":
    main()
