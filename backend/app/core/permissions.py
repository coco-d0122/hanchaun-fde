# -*- coding: utf-8 -*-
"""
权限码与角色定义（阶段5第一批）

数据来源：《阶段5 管理后台与权限设计方案 v1.0》第三节权限矩阵。
本文件是种子数据的唯一来源；启动时同步进 roles / permissions /
role_permissions 三张表（幂等），角色→权限码集合缓存在 app.state。

★ 关键立场：`anomaly:review:submit`（业务签核权）不授予任何角色——
  admin 也没有。"系统管理员 ≠ 电气安全主管"，签核权未来只授予业务角色。
"""

from __future__ import annotations

# 权限码字典（code → 描述，scope_required 标记是否需叠加数据范围判断）
PERMISSIONS: dict[str, tuple[str, bool]] = {
    "chat:use": ("使用 AI 聊天问答", False),
    "session:read:own": ("查看自己的会话历史", False),
    "anomaly:read": ("查看异常事件与证据", True),
    # 业务签核权：本阶段任何角色都没有（含 admin）
    "anomaly:review:submit": ("提交正式复核结论（签核）", True),
    "user:read": ("查看用户列表", False),
    "user:write": ("创建用户 / 修改角色 / 禁用启用", False),
    "kb:read": ("查看知识库文档", False),
    "kb:write": ("上传文档 / 重新处理", False),
    "kb:delete": ("删除知识库文档", False),
    "prompt:read": ("查看提示词模板", False),
    "prompt:write": ("修改提示词模板 / 恢复历史版本", False),
    "audit:conversation:read": ("查看对话审计记录", False),
    "audit:flag:write": ("添加与管理风险标记", False),
    "log:read": ("查看系统操作日志", False),
}

# 角色定义（code → (中文名, 描述)）
ROLES: dict[str, tuple[str, str]] = {
    "user": ("普通用户", "使用聊天问答、查看授权范围内异常事件与自己 的会话"),
    "admin": ("系统管理员", "系统管理权（用户/知识库/提示词/审计/日志），不含业务签核权"),
}

# 角色 → 权限码集合
ROLE_PERMISSIONS: dict[str, list[str]] = {
    "user": [
        "chat:use",
        "session:read:own",
        "anomaly:read",
    ],
    "admin": [
        "chat:use",
        "session:read:own",
        "anomaly:read",
        # 注意：没有 anomaly:review:submit —— 有意为之，见文件头注释
        "user:read",
        "user:write",
        "kb:read",
        "kb:write",
        "kb:delete",
        "prompt:read",
        "prompt:write",
        "audit:conversation:read",
        "audit:flag:write",
        "log:read",
    ],
}
