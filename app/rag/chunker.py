# -*- coding: utf-8 -*-
"""
文本切片策略：把长文切成可检索、可溯源的块（纯函数，无 IO）。

为什么单独成文件：切片参数的调整（chunk_size / overlap / 标题路径）会直接影响
检索命中率与引用质量，把它与"读文件"分开，才能单独测、单独调。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


@dataclass
class Chunk:
    """一个可检索的文本切片及其全部溯源元数据。"""

    chunk_id: str            # 全局唯一：{文件名}::c{序号}
    content: str
    source_file: str         # 相对知识库根目录的文件名
    doc_title: str           # 文档标题（一级标题）
    heading_path: str        # 标题路径，如 "2 倍率管理 > 2.2 变更流程"
    chunk_index: int         # 文档内块序号（从 1 开始）
    char_start: int          # 在原文中的起始字符偏移（便于人工复核定位）
    char_end: int

    def citation(self) -> str:
        """生成人可读的引用标签，用于在回答中标注来源。"""
        return f"{self.source_file} · {self.heading_path} · 块{self.chunk_index}"

    def index_text(self) -> str:
        """
        用于建索引的文本 = 标题路径 + 正文。

        为什么要拼标题：切片正文里往往不出现"总表""分表"这类主题词
        （它们在文档标题里），不拼的话 BM25 与向量都会漏召。
        注意：拼进去的标题只用于"匹配"，展示与喂给大模型时仍用纯正文 content。
        """
        return f"{self.doc_title} · {self.heading_path}\n{self.content}"

def _split_long_paragraph(text: str, chunk_size: int, overlap: int) -> list[str]:
    """单段文字超过 chunk_size 时按窗口硬切，保留 overlap 重叠，避免语义被拦腰截断。"""
    pieces: list[str] = []
    step = max(1, chunk_size - overlap)
    for start in range(0, len(text), step):
        piece = text[start:start + chunk_size]
        if piece.strip():
            pieces.append(piece.strip())
        if start + chunk_size >= len(text):
            break
    return pieces

def pack_paragraphs(text: str, chunk_size: int, overlap: int) -> list[str]:
    """
    按段落把正文打包成接近 chunk_size 的切片（核心切片算法）。

    为什么按段落而不是定长切？
      定长切会把"倍率变更需填写《计量配置变更单》"这种完整句子切成两半，
      检索命中后上下文残缺，喂给大模型反而制造幻觉。

    算法：
      1. 按空行拆成段落
      2. 逐段往 buffer 里塞，塞不下就落盘当前 buffer
      3. 新 buffer 用"上一块末尾 overlap 个字符"作为前缀，保证跨块语义连续
      4. 超长段落单独硬切
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    buffer = ""

    for para in paragraphs:
        # 超长段落：先落盘当前 buffer，再硬切
        if len(para) > chunk_size:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.extend(_split_long_paragraph(para, chunk_size, overlap))
            continue

        if not buffer:
            buffer = para
        elif len(buffer) + len(para) + 1 <= chunk_size:
            buffer = f"{buffer}\n{para}"
        else:
            chunks.append(buffer)
            tail = buffer[-overlap:] if overlap > 0 else ""
            buffer = f"{tail}\n{para}".strip() if tail else para

    if buffer:
        chunks.append(buffer)
    return chunks

def _pieces_of_paragraph(para: str, chunk_size: int, overlap: int) -> list[str]:
    """单段落 → 0..N 个切片（超长硬切，否则整段）。"""
    if len(para) > chunk_size:
        return _split_long_paragraph(para, chunk_size, overlap)
    return [para]

def _sections_to_chunks(sections: list[tuple[str, str, int]], rel_name: str,
                        doc_title: str, stem: str,
                        chunk_size: int, overlap: int) -> list[Chunk]:
    """
    "节"列表 → 合并成接近 chunk_size 的切片（公共实现）。

    为什么要合并：Markdown 的小节往往只有一两百字，若"一节=一块"，
    切片过碎会让单个片段缺少上下文，既降低检索质量，也让大模型看到的依据残缺。
    合并后每块约 500 字，通常覆盖 2~4 个相邻小节，语义更完整。
    """
    all_chunks: list[Chunk] = []
    idx = 0

    def _emit(path: str, text: str, offset: int) -> None:
        nonlocal idx
        idx += 1
        all_chunks.append(Chunk(
            chunk_id=f"{Path(rel_name).stem}::c{idx:03d}",
            content=text.strip(),
            source_file=rel_name,
            doc_title=doc_title,
            heading_path=path,
            chunk_index=idx,
            char_start=offset,
            char_end=offset + len(text),
        ))

    buffer_text = ""
    buffer_path = ""
    buffer_offset = 0

    for heading_path, body, offset in sections:
        # 单节超长：先落盘当前缓冲，再对超长节单独按段落打包
        if len(body) > chunk_size:
            if buffer_text:
                _emit(buffer_path, buffer_text, buffer_offset)
                buffer_text, buffer_path = "", ""
            for piece in pack_paragraphs(body, chunk_size, overlap):
                _emit(heading_path, piece, offset)
            continue

        if not buffer_text:
            buffer_text, buffer_path, buffer_offset = body, heading_path, offset
        elif len(buffer_text) + len(body) + 2 <= chunk_size:
            buffer_text = f"{buffer_text}\n\n{body}"
        else:
            _emit(buffer_path, buffer_text, buffer_offset)
            buffer_text, buffer_path, buffer_offset = body, heading_path, offset

    if buffer_text:
        _emit(buffer_path or stem, buffer_text, buffer_offset)

    return all_chunks
