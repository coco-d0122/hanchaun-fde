# -*- coding: utf-8 -*-
"""
安全组件（阶段5第一批）：密码哈希与 JWT

设计要点（方案 3.3 / D4 / D5）：
  - 密码：bcrypt cost=12，库中只存 $2b$ 前缀的哈希，绝不存明文
  - JWT：HS256，有效期 8 小时（工作班次长度，过期重登，不做 refresh token）
  - 密钥：JWT_SECRET 环境变量优先；未配置时生成随机密钥并持久化到
    仓库根目录 .jwt_secret（gitignore），避免每次重启把所有人登出
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import bcrypt
import jwt

from app.core.config import BACKEND_DIR

# bcrypt cost：12 是 2026 年服务器的合理平衡点（单次哈希约 250ms）
_BCRYPT_ROUNDS = 12
# JWT 有效期（小时）——D4 默认：8 小时
JWT_EXPIRE_HOURS = 8
JWT_ALGORITHM = "HS256"
COOKIE_NAME = "hc_token"


# ---------------- 密码 ----------------

def hash_password(plain: str) -> str:
    """bcrypt 哈希（自动加盐，输出 $2b$12$... 格式）。"""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("ascii")


def verify_password(plain: str, password_hash: str) -> bool:
    """校验密码。恒定时间比较由 bcrypt 内部保证；异常一律返回 False。"""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


# ---------------- JWT ----------------

def _secret_file() -> Path:
    return BACKEND_DIR / ".jwt_secret"


def get_jwt_secret() -> str:
    """
    JWT 签名密钥：env 优先 → 持久化随机文件 → 生成并落盘。

    为什么不用"每次启动随机"：那会让所有用户在服务重启后被登出，
    破坏"8 小时会话"的预期。
    """
    import os
    env_secret = (os.getenv("JWT_SECRET") or "").strip()
    if env_secret:
        return env_secret

    f = _secret_file()
    if f.exists():
        stored = f.read_text(encoding="utf-8").strip()
        if stored:
            return stored

    generated = secrets.token_urlsafe(48)
    f.write_text(generated, encoding="utf-8")
    return generated


def issue_token(user_id: int, username: str, role: str) -> tuple[str, datetime]:
    """签发 JWT。payload 只放最小必要信息，权限码在服务端按角色实时解析。"""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=JWT_EXPIRE_HOURS)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)
    return token, expires


def decode_token(token: str) -> dict[str, Any] | None:
    """
    校验并解码 JWT。签名无效 / 过期 / 结构异常一律返回 None（由调用方统一转 401）。

    不在 payload 里放权限码：权限以数据库角色为准，改角色后立即生效，
    避免旧 token 携带过期权限。
    """
    try:
        return jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
