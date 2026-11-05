# -*- coding: utf-8 -*-
"""
知识库管理服务（阶段5第二批）

核心职责：
  1. 上传流水线：安全校验（扩展名/魔数/大小）→ 落盘 → 解析 → 切片 → 向量化
     → 版本落库 → 影子重建检索器 → 原子切换。
  2. 删除（软删）与重新处理。
  3. 内建文档种子化：knowledge/samples/ 下 6 份模拟规则文档首次启动时登记进 DB，
     之后"知识库内容"的唯一事实来源就是 knowledge_documents 表。

★ 安全边界（对应设计方案 5.1 节）：
  - 解析/切片/向量化任何一步失败 → 文档置 failed，线上索引从未被触碰；
  - PDF 无文本层 → status=needs_ocr，流程终止并返回中文提示；
  - .exe 改名 .pdf → 魔数校验拒绝（%PDF- 头）。
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from fastapi import UploadFile

from app.core.config import Settings
from app.rag.embeddings import build_embedder
from app.rag.retriever import DocEntry, HybridRetriever
from app.db import repo
from app.db.models import KnowledgeDocument, KnowledgeDocumentVersion
from app.db.session import session_factory
from app.errors import AppError

logger = logging.getLogger(__name__)

# 文档类型枚举（6 类，对应设计方案三节）
DOC_TYPES = [
    ("policy", "园区能源管理制度"),
    ("metering", "电表计量管理规范"),
    ("ownership", "设备与计量点归属说明"),
    ("troubleshooting", "能耗异常排查手册"),
    ("review", "人工复核与安全管理规范"),
    ("case", "历史异常案例"),
    ("other", "其他"),
]

# 内建文档 → doc_type 映射（按文件名前缀）
_BUILTIN_TYPE_MAP = {
    "01": "policy", "02": "metering", "03": "metering",
    "04": "ownership", "05": "troubleshooting", "06": "review",
}

_PDF_MAGIC = b"%PDF-"


# ---------------------------------------------------------------- PDF 解析
def extract_pdf_text(pdf_path: Path) -> str:
    """
    用 pypdf 提取 PDF 文本层（D2 默认选型：纯 Python、无系统依赖）。

    返回空字符串 = 无文本层（扫描件）——调用方据此置 needs_ocr。
    任何解析异常都向上抛，由上传流水线统一置 failed。
    """
    from pypdf import PdfReader     # 延迟导入：不装 pypdf 的环境仅内建 md 也能跑

    reader = PdfReader(str(pdf_path))
    texts: list[str] = []
    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception:   # noqa: BLE001  单页失败不拖垮整份文档
            continue
    return "\n\n".join(t for t in texts if t.strip())


# ---------------------------------------------------------------- 安全校验
def validate_upload(file_bytes: bytes, filename: str, cfg: Settings) -> None:
    """
    上传三道校验：扩展名白名单 → 大小限制 → 文件头魔数。

    魔数校验防"改后缀冒充"：.exe/.zip 改名 .pdf 头部不是 %PDF-，直接拒绝。
    """
    ext = Path(filename).suffix.lower()
    if ext not in cfg.kb_allowed_exts:
        raise AppError("KB_BAD_FILE_TYPE",
                       f"仅支持 {' / '.join(cfg.kb_allowed_exts)} 格式，收到 {ext or '(无扩展名)'}", 400)

    if len(file_bytes) > cfg.kb_upload_max_mb * 1024 * 1024:
        raise AppError("KB_FILE_TOO_LARGE",
                       f"文件超过 {cfg.kb_upload_max_mb}MB 上限", 413)

    if ext == ".pdf" and not file_bytes.startswith(_PDF_MAGIC):
        raise AppError("KB_BAD_FILE_TYPE",
                       "文件内容不是有效的 PDF（文件头校验失败，疑似改名文件）", 400)

    if ext == ".md":
        # md 是纯文本：必须能按 UTF-8 解码（容错替换也行，但完全二进制内容拒绝）
        try:
            file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                file_bytes.decode("gbk")
            except UnicodeDecodeError:
                raise AppError("KB_BAD_FILE_TYPE",
                               "Markdown 文件无法按文本解码，请确认文件内容", 400)


# ---------------------------------------------------------------- 文本抽取
def extract_text(file_path: Path) -> str:
    """按扩展名抽取文本层（md 直读，pdf 走 pypdf）。"""
    if file_path.suffix.lower() == ".md":
        return file_path.read_text(encoding="utf-8", errors="replace")
    return extract_pdf_text(file_path)


# ---------------------------------------------------------------- 种子内建文档
async def seed_builtin_documents(cfg: Settings) -> int:
    """
    首次启动：把知识库目录下的内建文档登记进 knowledge_documents。

    幂等：表非空直接返回。内建文档 uploaded_by=NULL（系统）、file_path 指向
    原位置（不复制）。此后检索构建只认 DB 清单。
    """
    async with session_factory() as db:
        async with db.begin():
            existing = await repo.count_kb_documents(db)
            if existing:
                return 0

            created = 0
            for md in sorted(cfg.kb_path.glob("*.md")):
                content_hash = hashlib.sha256(md.read_bytes()).hexdigest()[:16]
                prefix = md.stem[:2]
                doc = KnowledgeDocument(
                    filename=md.name,
                    doc_type=_BUILTIN_TYPE_MAP.get(prefix, "other"),
                    status="published",
                    file_path=f"{cfg.kb_dir}/{md.name}",
                    error_info="",
                    uploaded_by=None,
                )
                db.add(doc)
                await db.flush()
                # 切片数在首次构建时无法预知——先记 0，构建后回填
                ver = KnowledgeDocumentVersion(
                    document_id=doc.id,
                    version_no=1,
                    chunk_count=0,
                    content_hash=content_hash,
                    status="published",
                )
                db.add(ver)
                await db.flush()
                doc.current_version_id = ver.id
                created += 1
            return created


async def backfill_chunk_counts(retriever: HybridRetriever) -> int:
    """构建完成后把实际切片数回填到版本表（内建种子时记的是 0）。"""
    by_file: dict[str, int] = {}
    for c in retriever.chunks:
        by_file[c.source_file] = by_file.get(c.source_file, 0) + 1
    if not by_file:
        return 0
    async with session_factory() as db:
        async with db.begin():
            return await repo.update_version_chunk_counts(db, by_file)


# ---------------------------------------------------------------- 检索器重建
async def load_doc_entries(include_ids: list[int] | None = None,
                           exclude_ids: list[int] | None = None) -> list[DocEntry]:
    """
    读取检索构建输入：已发布文档 + include_ids（上传/重处理中的目标文档）
    − exclude_ids（待删除文档）。

    include_ids 不可省：上传流水线里新文档还是 processing 状态，
    只读 published 会让这份文档被排除在影子构建之外（切片数为 0 的经典坑）。
    exclude_ids 同理：删除时文档尚未落 deleted，必须显式剔除。
    """
    async with session_factory() as db:
        async with db.begin():
            rows = await repo.list_kb_documents_for_build(
                db, include_ids or [], exclude_ids or [])
    project_root = Path(__file__).resolve().parents[2]
    return [
        DocEntry(
            doc_id=r["id"],
            # 库里存的是相对仓库根目录的路径（内建：knowledge/samples/x.md；上传：uploads/docN/vN/x.md），
            # 绝对化放在读取处，数据库记录因此可以跨机器/容器迁移。
            file_path=project_root / r["file_path"],
            display_name=r["filename"],
            content_hash=r["content_hash"] or "",
        )
        for r in rows
    ]


async def rebuild_retriever(app, cfg: Settings,
                            include_ids: list[int] | None = None,
                            exclude_ids: list[int] | None = None) -> HybridRetriever:
    """
    影子重建 + 原子切换（设计方案 5.1 节第 ①⑦ 步的实现）。

    在全新的 HybridRetriever 实例上构建（影子）；
    任何一步失败直接抛异常，旧对象保持服务；
    全部成功后唯一一行赋值完成切换——Python 引用替换是原子的，
    在途请求仍持有旧对象引用，互不干扰。

    include_ids：把"尚未发布但需要一起参与构建"的文档（上传/重处理目标）纳入，
    构建成功后才由调用方置 published；构建失败则影子整体丢弃。
    exclude_ids：把"尚未落 deleted 但需要从索引剔除"的文档排除，删除流水线用。

    阻塞操作（切片/向量化）经 anyio.to_thread 走线程池，不卡事件循环。
    """
    import anyio

    entries = await load_doc_entries(include_ids, exclude_ids)
    if not entries:
        raise AppError("KB_EMPTY", "知识库为空（没有已发布文档）", 500)

    embedder = build_embedder(cfg)
    shadow = HybridRetriever(cfg, embedder)

    def _build() -> None:
        shadow.build_from_documents(entries)

    await anyio.to_thread.run_sync(_build)

    # ---- 原子切换：唯一一次对 app.state.retriever 的写 ----
    app.state.retriever = shadow
    await backfill_chunk_counts(shadow)
    return shadow


# ---------------------------------------------------------------- 上传流水线
async def process_upload(
    app, cfg: Settings, file_bytes: bytes, filename: str,
    doc_type: str, user_id: int,
) -> KnowledgeDocument:
    """
    上传 → 发布全流水线。成功返回已发布的文档对象（含新版本号）。

    失败语义：
      - 校验类失败（类型/大小/魔数）→ AppError 4xx，不产生 DB 记录；
      - 解析/向量化失败 → 文档置 failed + error_info，索引不受影响；
      - PDF 无文本层 → needs_ocr + 中文提示，索引不受影响。
    """
    filename = Path(filename).name                    # 防路径穿越：只留文件名
    filename = re.sub(r"[\\/:*?\"<>|]", "_", filename)[:128] or "未命名文档.md"
    validate_upload(file_bytes, filename, cfg)

    content_hash = hashlib.sha256(file_bytes).hexdigest()[:16]

    # ---- ① 原始文件落盘 uploads/{doc_id}/{version_no}/ ----
    async with session_factory() as db:
        async with db.begin():
            doc = KnowledgeDocument(
                filename=filename,
                doc_type=doc_type if doc_type in {t for t, _ in DOC_TYPES} else "other",
                status="processing",
                file_path="",                      # 落盘后回填
                error_info="",
                uploaded_by=user_id,
            )
            db.add(doc)
            await db.flush()
            # 版本号 = 该文档已有最大版本 + 1（新上传文档为 1）
            max_no = await repo.max_kb_version_no(db, doc.id)
            ver = KnowledgeDocumentVersion(
                document_id=doc.id,
                version_no=(max_no or 0) + 1,
                chunk_count=0,
                content_hash=content_hash,
                status="processing",
            )
            db.add(ver)
            await db.flush()
            doc.current_version_id = ver.id
            doc_id, version_no = doc.id, ver.version_no

    rel_dir = f"uploads/doc{doc_id}/v{version_no}"
    abs_dir = cfg.uploads_path / f"doc{doc_id}" / f"v{version_no}"
    abs_dir.mkdir(parents=True, exist_ok=True)
    (abs_dir / filename).write_bytes(file_bytes)

    async with session_factory() as db:
        async with db.begin():
            await repo.set_kb_file_path(db, doc_id, f"{rel_dir}/{filename}")

    # ---- ② 解析：PDF 无文本层 → needs_ocr，流程终止 ----
    file_path = abs_dir / filename
    try:
        if file_path.suffix.lower() == ".pdf":
            text = extract_pdf_text(file_path)
            if not text.strip():
                async with session_factory() as db:
                    async with db.begin():
                        await repo.set_kb_status(db, doc_id, "needs_ocr",
                                                 "PDF 疑似扫描件：无法提取文本层")
                raise AppError("KB_NEEDS_OCR",
                               "该 PDF 疑似扫描件，无法直接提取文本，"
                               "需先 OCR 处理后重新上传", 422)
    except AppError:
        raise
    except Exception as exc:   # noqa: BLE001  解析失败 → failed，索引不受影响
        await _mark_failed(doc_id, f"PDF 解析失败：{exc}")
        raise AppError("KB_PARSE_FAILED", f"文档解析失败：{exc}", 422) from exc

    # ---- ③ 影子重建 + 原子切换（切片与向量化在 rebuild 内完成）----
    try:
        shadow = await rebuild_retriever(app, cfg, include_ids=[doc_id])
    except AppError:
        raise
    except Exception as exc:   # noqa: BLE001
        await _mark_failed(doc_id, f"索引构建失败：{exc}")
        raise AppError("KB_INDEX_FAILED", f"知识库索引构建失败：{exc}", 500) from exc

    # ---- ④ 发布：文档与版本置 published，回填切片数，写审计 ----
    chunk_count = sum(1 for c in shadow.chunks if c.source_file == filename)
    async with session_factory() as db:
        async with db.begin():
            await repo.set_kb_status(db, doc_id, "published", error_info="")
            await repo.set_kb_version_published(db, doc_id, content_hash, chunk_count)
            await repo.write_audit_log(
                db, "kb_upload", user_id,
                object_type="kb_document", object_id=str(doc_id),
                detail={"filename": filename, "doc_type": doc_type,
                        "version_no": version_no, "chunks": chunk_count})
    logger.info("文档发布成功：#%s %s（%s 切片）", doc_id, filename, chunk_count)

    async with session_factory() as db:
        async with db.begin():
            doc = await repo.get_kb_document(db, doc_id)
    return doc


async def _mark_failed(doc_id: int, error_info: str) -> None:
    """文档置 failed（中文错误说明，截断到 500 字）。"""
    async with session_factory() as db:
        async with db.begin():
            await repo.set_kb_status(db, doc_id, "failed", error_info[:500])


# ---------------------------------------------------------------- 删除
def is_builtin_path(file_path: str | None) -> bool:
    """内建文档 = 代码库里的知识库文件；上传文档一律在 uploads/ 下。"""
    return bool(file_path) and not (file_path or "").startswith("uploads/")


async def delete_document(app, cfg: Settings, doc_id: int, user_id: int) -> None:
    """
    软删除：status=deleted（行保留供审计），检索内容立即移除。

    ★ 内建文档不允许删除（file_path 指向 knowledge/samples/，属于代码库资产）。
    """
    async with session_factory() as db:
        async with db.begin():
            doc = await repo.get_kb_document(db, doc_id)
            if doc is None:
                raise AppError("KB_DOC_NOT_FOUND", "知识库文档不存在", 404)
            if doc.status == "deleted":
                raise AppError("KB_DOC_NOT_FOUND", "该文档已被删除", 404)
            if is_builtin_path(doc.file_path):
                raise AppError("KB_BUILTIN_PROTECTED",
                               "内建文档不可删除（如需下线请在配置中移除后重启）", 403)

    # ---- 影子重建（剔除该文档后）+ 原子切换 ----
    # 必须用 exclude_ids：此刻文档还是 published，不排除就会被重新带进新索引
    try:
        await rebuild_retriever(app, cfg, exclude_ids=[doc_id])
    except AppError:
        raise
    except Exception as exc:   # noqa: BLE001  切换失败 → 文档状态不变，索引不变
        raise AppError("KB_INDEX_FAILED", f"知识库索引重建失败：{exc}", 500) from exc

    async with session_factory() as db:
        async with db.begin():
            await repo.set_kb_status(db, doc_id, "deleted", "")
            await repo.write_audit_log(
                db, "kb_delete", user_id,
                object_type="kb_document", object_id=str(doc_id),
                detail={"filename": doc.filename})


# ---------------------------------------------------------------- 重新处理
async def reprocess_document(app, cfg: Settings, doc_id: int, user_id: int) -> None:
    """
    重新处理当前版本：从原始文件重跑 解析→切片→向量化→发布。

    content_hash 未变时按文档缓存直接复用向量（幂等、秒级）。
    failed / needs_ocr / published 均可重处理。
    """
    backend_root = Path(__file__).resolve().parents[2]
    async with session_factory() as db:
        async with db.begin():
            doc = await repo.get_kb_document(db, doc_id)
            if doc is None or doc.status == "deleted":
                raise AppError("KB_DOC_NOT_FOUND", "知识库文档不存在或已删除", 404)

    file_path = Path(__file__).resolve().parents[2] / doc.file_path
    if not file_path.exists():
        await _mark_failed(doc_id, "原始文件丢失，无法重新处理")
        raise AppError("KB_FILE_MISSING", "原始文件已不存在，无法重新处理", 422)

    if file_path.suffix.lower() == ".pdf":
        try:
            if not extract_pdf_text(file_path).strip():
                async with session_factory() as db:
                    async with db.begin():
                        await repo.set_kb_status(db, doc_id, "needs_ocr",
                                                 "PDF 疑似扫描件：无法提取文本层")
                raise AppError("KB_NEEDS_OCR",
                               "该 PDF 疑似扫描件，无法直接提取文本，需先 OCR 处理", 422)
        except AppError:
            raise
        except Exception as exc:   # noqa: BLE001
            await _mark_failed(doc_id, f"PDF 解析失败：{exc}")
            raise AppError("KB_PARSE_FAILED", f"文档解析失败：{exc}", 422) from exc

    try:
        shadow = await rebuild_retriever(app, cfg, include_ids=[doc_id])
    except AppError:
        raise
    except Exception as exc:   # noqa: BLE001
        await _mark_failed(doc_id, f"索引构建失败：{exc}")
        raise AppError("KB_INDEX_FAILED", f"知识库索引构建失败：{exc}", 500) from exc

    chunk_count = sum(1 for c in shadow.chunks if c.source_file == doc.filename)
    async with session_factory() as db:
        async with db.begin():
            content_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()[:16]
            await repo.set_kb_status(db, doc_id, "published", "")
            await repo.set_kb_version_published(db, doc_id, content_hash, chunk_count)
            await repo.write_audit_log(
                db, "kb_reindex", user_id,
                object_type="kb_document", object_id=str(doc_id),
                detail={"filename": doc.filename, "chunks": chunk_count})
    logger.info("文档重新处理完成：#%s %s（%s 切片）", doc_id, doc.filename, chunk_count)
