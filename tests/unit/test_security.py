# -*- coding: utf-8 -*-
"""认证与权限：bcrypt 存储、JWT 篡改拒绝、管理员不含业务签核权。"""

from __future__ import annotations

import jwt

from app.core import permissions
from app.core.security import (
    JWT_ALGORITHM,
    decode_token,
    get_jwt_secret,
    hash_password,
    issue_token,
    verify_password,
)


def test_password_is_hashed_not_plaintext():
    h = hash_password("TestPass12345")
    assert h.startswith("$2b$")
    assert "TestPass12345" not in h
    assert verify_password("TestPass12345", h) is True
    assert verify_password("wrong-password", h) is False


def test_jwt_roundtrip_and_tamper_rejected():
    token, _ = issue_token(7, "tester", "user")
    payload = decode_token(token)
    assert payload and payload["sub"] == "7" and payload["role"] == "user"

    # 用错误密钥重签：必须被拒绝（防伪造管理员令牌）
    forged = jwt.encode({**payload, "role": "admin"}, "not-the-secret",
                        algorithm=JWT_ALGORITHM)
    assert decode_token(forged) is None

    # 结构损坏：不得抛异常，返回 None
    assert decode_token("not.a.jwt") is None


def test_admin_has_no_business_signoff_permission():
    """
    "系统管理员 ≠ 电气安全主管"必须由权限表保证，而不是靠提示词或页面隐藏。

    当前实现更严格：任何角色都没有 anomaly:review:submit（签核只能线下）。
    """
    granted_anywhere = {p for codes in permissions.ROLE_PERMISSIONS.values() for p in codes}
    assert "anomaly:review:submit" not in granted_anywhere
    assert "anomaly:review:submit" not in permissions.ROLE_PERMISSIONS["admin"]


def test_admin_permissions_are_subset_of_defined_codes():
    defined = set(permissions.PERMISSIONS)
    for role, codes in permissions.ROLE_PERMISSIONS.items():
        assert set(codes) <= defined, f"{role} 出现了未定义的权限码"


def test_jwt_secret_not_empty():
    assert len(get_jwt_secret()) >= 16
