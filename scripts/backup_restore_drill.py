# -*- coding: utf-8 -*-
"""
备份恢复演练脚本（阶段11，对应自检清单 N-07 / 阶段9 D5-02）

流程（全程只操作副本，绝不触碰线上库文件本身）：
  1. 备份：sqlite3 backup API 在线备份 hanchuan.db → 备份文件
  2. 校验：integrity_check + 关键表行数快照
  3. 恢复：在"工作副本"上模拟灾难（删除全部业务表）→ 从备份恢复 → 比对行数
  4. 输出耗时与一致性结论；结果写入 eval/restore_drill_report.json

用法：python scripts/backup_restore_drill.py [--db hanchuan.db]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 行数快照的业务表（恢复后必须逐一相等）
CHECK_TABLES = ("users", "conversations", "messages", "anomaly_events",
                "anomaly_evidence", "agent_tasks", "agent_tool_calls",
                "agent_report_drafts", "knowledge_documents", "audit_logs")


def table_counts(con: sqlite3.Connection) -> dict[str, int]:
    existing = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    out: dict[str, int] = {}
    for t in CHECK_TABLES:
        out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if t in existing else -1
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "hanchuan.db"))
    args = ap.parse_args()
    src = Path(args.db)
    if not src.exists():
        raise SystemExit(f"数据库不存在：{src}（先启动一次应用完成建表再演练）")

    workdir = ROOT / "eval" / "drill_tmp"
    workdir.mkdir(parents=True, exist_ok=True)
    backup = workdir / "hanchuan_backup.db"
    work = workdir / "hanchuan_work.db"

    report: dict = {"db": str(src), "steps": []}

    # ---- 1) 在线备份（backup API：不锁库，WAL 安全）----
    t0 = time.perf_counter()
    dst = sqlite3.connect(str(backup))
    source = sqlite3.connect(str(src))
    source.backup(dst)
    dst.close()
    source.close()
    report["steps"].append({"step": "backup", "seconds": round(time.perf_counter() - t0, 3),
                            "file": str(backup)})

    # ---- 2) 备份完整性 + 行数快照 ----
    con = sqlite3.connect(str(backup))
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    counts_before = table_counts(con)
    con.close()
    report["counts_before"] = counts_before

    # ---- 3) 灾难模拟：把工作副本的业务表全部删掉 ----
    shutil.copy(backup, work)
    con = sqlite3.connect(str(work))
    for t in CHECK_TABLES:
        con.execute(f"DROP TABLE IF EXISTS {t}")
    con.commit()
    assert table_counts(con)[CHECK_TABLES[0]] == -1, "灾难模拟失败：表未删除"
    con.close()

    # ---- 4) 恢复：从备份覆盖工作副本 ----
    t0 = time.perf_counter()
    shutil.copy(backup, work)
    report["steps"].append({"step": "restore", "seconds": round(time.perf_counter() - t0, 3)})

    con = sqlite3.connect(str(work))
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    counts_after = table_counts(con)
    con.close()
    report["counts_after"] = counts_after
    report["consistent"] = counts_before == counts_after
    report["generated_at"] = datetime.now(timezone.utc).isoformat()

    out = ROOT / "eval" / "restore_drill_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({**report, "steps": report["steps"]},
                     ensure_ascii=False, indent=2))
    print("\n结论：", "恢复成功，行数一致 ✓" if report["consistent"]
          else "恢复后行数不一致 ✗（不得上线）")
    print(f"报告已写入：{out}")

    # 清理工作副本（保留备份文件供人工核验）
    work.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
