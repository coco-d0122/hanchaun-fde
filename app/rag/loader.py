# -*- coding: utf-8 -*-
"""
知识库文件加载：Markdown / 纯文本 → 带溯源元数据的切片列表。

切片策略在 chunker.py，本模块只负责"读文件、认标题、组装来源信息"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


from app.rag.chunker import (Chunk, _pieces_of_paragraph, _sections_to_chunks,
                            _split_long_paragraph, pack_paragraphs)


def load_markdown_chunks(kb_dir: Path, chunk_size: int, overlap: int) -> list[Chunk]:
    """
    加载知识库目录下所有 .md 文件并切片（阶段3/4 的目录级入口，保留兼容）。
    """
    if not kb_dir.exists():
        raise FileNotFoundError(f"知识库目录不存在：{kb_dir}")

    md_files = sorted(kb_dir.rglob("*.md"))
    if not md_files:
        raise FileNotFoundError(f"知识库目录下没有 .md 文件：{kb_dir}")

    all_chunks: list[Chunk] = []
    for md_path in md_files:
        rel_name = md_path.relative_to(kb_dir).as_posix()
        all_chunks.extend(markdown_file_chunks(md_path, rel_name, chunk_size, overlap))
    return all_chunks

def markdown_file_chunks(md_path: Path, rel_name: str,
                          chunk_size: int, overlap: int) -> list[Chunk]:
    """
    单个 Markdown 文件 → 切片（阶段5第二批：知识库管理按文档独立处理）。

    标题处理：维护一个标题栈，遇到新标题就更新路径；
              这样每个切片都能带上"2 倍率管理 > 2.2 变更流程"这样的路径。
    """
    raw = md_path.read_text(encoding="utf-8", errors="replace")

    # --- 1) 按标题切分为"节"，同时记录每节在原文中的偏移 ---
    lines = raw.splitlines()
    sections: list[tuple[str, str, int]] = []   # (heading_path, body, char_offset)
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    sec_start = 0
    cursor = 0

    def _flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            path = " > ".join(t for _, t in stack) if stack else md_path.stem
            sections.append((path, body, sec_start))

    for line in lines:
        m = HEADING_RE.match(line)
        if m:
            _flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            buf = []
            sec_start = cursor + len(line) + 1
        else:
            # 跳过引用块（> 开头的行）。知识库文档顶部的"模拟规则"声明属于
            # 文档元信息而非规则正文，若进入索引会被检索命中并当作依据返回，
            # 既污染上下文又浪费 token。
            if not line.lstrip().startswith(">"):
                buf.append(line)
        cursor += len(line) + 1
    _flush()

    # --- 2) 文档标题 = 第一个一级标题 ---
    doc_title = md_path.stem
    for path, _, _ in sections:
        first = path.split(" > ")[0]
        if first:
            doc_title = first
            break

    return _sections_to_chunks(sections, rel_name, doc_title, md_path.stem,
                               chunk_size, overlap)

def plain_text_chunks(text: str, source_name: str, doc_title: str,
                      chunk_size: int, overlap: int) -> list[Chunk]:
    """
    无结构纯文本（PDF 文本层等）→ 切片（阶段5第二批）。

    没有标题可用，heading_path 置空，直接按段落打包；
    引用块过滤不适用（PDF 提取文本没有 Markdown 语法）。
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return []

    pieces: list[tuple[str, int]] = []   # (切片文本, 起始偏移)
    offset = 0
    for para in paragraphs:
        for piece in _pieces_of_paragraph(para, chunk_size, overlap):
            pieces.append((piece, offset))
        offset += len(para) + 2

    stem = Path(source_name).stem
    return [
        Chunk(
            chunk_id=f"{stem}::c{i:03d}",
            content=piece.strip(),
            source_file=source_name,
            doc_title=doc_title,
            heading_path="",
            chunk_index=i,
            char_start=off,
            char_end=off + len(piece),
        )
        for i, (piece, off) in enumerate(pieces, start=1)
    ]
