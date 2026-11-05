# -*- coding: utf-8 -*-
"""
Markdown 知识库加载与切片（阶段3原样迁移，纯函数无 IO）
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


def load_markdown_chunks(kb_dir: Path, chunk_size: int, overlap: int) -> list[Chunk]:
    """
    加载知识库目录下所有 .md 文件并切片。

    标题处理：维护一个标题栈，遇到新标题就更新路径；
              这样每个切片都能带上"2 倍率管理 > 2.2 变更流程"这样的路径。
    """
    if not kb_dir.exists():
        raise FileNotFoundError(f"知识库目录不存在：{kb_dir}")

    md_files = sorted(kb_dir.rglob("*.md"))
    if not md_files:
        raise FileNotFoundError(f"知识库目录下没有 .md 文件：{kb_dir}")

    all_chunks: list[Chunk] = []

    for md_path in md_files:
        raw = md_path.read_text(encoding="utf-8", errors="replace")
        rel_name = md_path.relative_to(kb_dir).as_posix()

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

        # --- 3) 把相邻小节点合并成接近 chunk_size 的切片 ---
        # 为什么要合并：Markdown 的小节往往只有一两百字，若"一节=一块"，
        # 切片过碎会让单个片段缺少上下文，既降低检索质量，也让大模型看到的依据残缺。
        # 合并后每块约 500 字，通常覆盖 2~4 个相邻小节，语义更完整。
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
            _emit(buffer_path or md_path.stem, buffer_text, buffer_offset)

    return all_chunks
