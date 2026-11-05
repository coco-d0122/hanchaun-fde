# -*- coding: utf-8 -*-
"""
RAG 依据判定：库内问题必须放行、库外/陷阱题必须拦截。

这是幻觉控制的第一道闸（缺陷 F-01 的回归测试）：
判定过松 → 拿无关片段编答案；过严 → 明明有制度也回答"没找到"。
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.rag.embeddings import build_embedder
from app.rag.retriever import HybridRetriever
from app.services.kb_service import DocEntry


@pytest.fixture(scope="module")
def retriever() -> HybridRetriever:
    """直接从 knowledge/samples 构建（不依赖数据库，测试可独立跑）。"""
    cfg = get_settings()
    entries = [
        DocEntry(doc_id=i, file_path=p, display_name=p.name, content_hash="")
        for i, p in enumerate(sorted(cfg.kb_path.glob("*.md")), start=1)
    ]
    r = HybridRetriever(cfg, build_embedder(cfg))
    r.build_from_documents(entries)
    return r


@pytest.mark.parametrize("question", [
    "电表倍率变更后需要核查哪些信息？",
    "总表与分表的计量规则是什么？",
    "能耗异常排查流程是怎样的？",
    "设备归属不明确时怎么处理？",
    "数据缺失应该怎么处理？",
    "哪些情况必须人工复核？",
])
def test_kb_questions_have_evidence(retriever, question):
    results = retriever.retrieve(question, top_k=3)
    assert results, "检索不应返回空"
    assert retriever.has_evidence(results, question) is True


@pytest.mark.parametrize("question", [
    "园区规定每台空调每天最多可以运行多少小时？",       # 知识库无此规定
    "租户逾期缴费的违约金比例是多少？",
    "电表的最大允许功率是多少千瓦？",
    "食堂今天有什么菜？",                                # 完全库外
    "园区停车费怎么收？",
])
def test_out_of_kb_questions_rejected(retriever, question):
    results = retriever.retrieve(question, top_k=3)
    # 关键：即使检索"能返回片段"，也不得判定为有依据
    assert retriever.has_evidence(results, question) is False


@pytest.mark.xfail(strict=False, reason="已知残留风险：库外问题出现高覆盖率词时可能放行（阶段8报告已登记）")
def test_annual_leave_question_rejected(retriever):
    """
    已知边界用例："公司年假多少天"与制度文本共享"公司/天/多少"等通用词，
    覆盖率可达 0.40，会被判为有依据。记录在案而不是假装通过。
    """
    results = retriever.retrieve("公司年假多少天？", top_k=3)
    assert retriever.has_evidence(results, "公司年假多少天？") is False


def test_evidence_ids_not_issued_without_evidence():
    """依据不足时不得签发证据编号——否则引用与报告会引用无关片段。"""
    from app.agent.tools import AgentContext, search_energy_knowledge
    from app.core.config import get_settings
    from app.rag.embeddings import build_embedder
    from app.rag.retriever import HybridRetriever
    from app.services.kb_service import DocEntry

    cfg = get_settings()
    entries = [DocEntry(doc_id=i, file_path=p, display_name=p.name, content_hash="")
               for i, p in enumerate(sorted(cfg.kb_path.glob("*.md")), start=1)]
    r = HybridRetriever(cfg, build_embedder(cfg))
    r.build_from_documents(entries)

    ctx = AgentContext(cfg=cfg, retriever=r, user_id=1)
    out = search_energy_knowledge(ctx, query="食堂今天有什么菜", top_k=3)
    assert out["has_evidence"] is False
    assert ctx.evidence == []
