# -*- coding: utf-8 -*-
"""课程交付 DOCX 生成工具：把本目录约定格式的 Markdown 转为规范 DOCX。

用法：
    python md2docx.py <input.md> <output.docx> --title <标题> --version V1.0 \
        --project "瀚川工业园能耗监测与异常定位智能体系统" --doc-code <编号>

支持：# ## ### 标题 / 段落（含 **粗体** 内联）/ - 与 1. 列表 / | 表格 |
> 引用块 / ![图](path) 图片 / 空行分段。
"""
from __future__ import annotations

import argparse
import re
from datetime import date

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor


def _set_cn_font(run, cn="宋体", en="Times New Roman"):
    run.font.name = en
    r = run._element.rPr
    r.rFonts.set(qn("w:eastAsia"), cn)


def _add_page_field(paragraph):
    for text, fld in (("第 ", None), (None, "PAGE"), (" 页 / 共 ", None), (None, "NUMPAGES"), (" 页", None)):
        if fld is None:
            run = paragraph.add_run(text)
            run.font.size = Pt(9)
            _set_cn_font(run)
        else:
            run = paragraph.add_run()
            run.font.size = Pt(9)
            for el in ("begin", "instrText", "end"):
                e = OxmlElement(f"w:fldChar" if el != "instrText" else "w:instrText")
                if el == "instrText":
                    e.text = f" {fld} "
                else:
                    e.set(qn("w:fldCharType"), el)
                run._element.append(e)
            _set_cn_font(run)


def _add_toc(document):
    paragraph = document.add_paragraph()
    run = paragraph.add_run()
    fld_begin = OxmlElement("w:fldChar"); fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText"); instr.set(qn("xml:space"), "preserve")
    instr.text = ' TOC \\o "1-3" \\h \\z \\u '
    fld_sep = OxmlElement("w:fldChar"); fld_sep.set(qn("w:fldCharType"), "separate")
    hint = OxmlElement("w:t"); hint.text = "（在 Word 中右键此处 → 更新域，即可生成目录）"
    fld_end = OxmlElement("w:fldChar"); fld_end.set(qn("w:fldCharType"), "end")
    for e in (fld_begin, instr, fld_sep, hint, fld_end):
        run._element.append(e)


def _inline(par, text, base_size=12):
    """处理 **粗体** 与 `代码` 内联标记。"""
    for token in re.split(r"(\*\*.+?\*\*|`[^`]+`)", text):
        if not token:
            continue
        if token.startswith("**") and token.endswith("**"):
            run = par.add_run(token[2:-2]); run.bold = True
        elif token.startswith("`") and token.endswith("`"):
            run = par.add_run(token[1:-1]); run.font.name = "Consolas"
            run.font.color.rgb = RGBColor(0x8B, 0x2F, 0x00)
        else:
            run = par.add_run(token)
        run.font.size = Pt(base_size)
        _set_cn_font(run)


def _heading(doc, text, level):
    h = doc.add_heading("", level=level)
    run = h.add_run(text)
    fonts = {1: ("黑体", 16), 2: ("黑体", 14), 3: ("黑体", 12.5), 4: ("楷体", 12)}
    cn, size = fonts.get(level, ("黑体", 12))
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor(0x1F, 0x3B, 0x57)
    _set_cn_font(run, cn=cn, en="Arial")
    return h


def _table(doc, rows):
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    # 第二行是分隔行
    body = [r for r in cells if not all(re.fullmatch(r":?-{2,}:?", c or "---") for c in r)]
    ncol = max(len(r) for r in body)
    t = doc.add_table(rows=len(body), cols=ncol)
    t.style = "Table Grid"
    for i, row in enumerate(body):
        for j in range(ncol):
            cell = t.cell(i, j)
            text = row[j] if j < len(row) else ""
            cell.text = ""
            par = cell.paragraphs[0]
            _inline(par, text.replace("**", ""), base_size=10)
            if i == 0:
                for run in par.runs:
                    run.bold = True
                # 表头底色
                shd = OxmlElement("w:shd"); shd.set(qn("w:fill"), "DCE6F1")
                cell._element.get_or_add_tcPr().append(shd)


def convert(md_path, out_path, title, version, project, doc_code):
    import os
    base_dir = os.path.dirname(os.path.abspath(md_path))
    doc = Document()
    # A4 与页边距
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.5)
    sec.top_margin, sec.bottom_margin = Cm(2.54), Cm(2.54)

    # 页眉
    hp = sec.header.paragraphs[0]
    hp.text = project
    hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in hp.runs:
        run.font.size = Pt(8); run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
        _set_cn_font(run)
    # 页脚页码
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _add_page_field(fp)

    # 封面
    for _ in range(5):
        doc.add_paragraph()
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(project); run.font.size = Pt(22); run.bold = True
    _set_cn_font(run, cn="黑体", en="Arial")
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(title); run.font.size = Pt(18); _set_cn_font(run, cn="黑体", en="Arial")
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(f"文档编号：{doc_code}　　版本：{version}　　日期：{date.today().isoformat()}")
    run.font.size = Pt(11); _set_cn_font(run)
    for _ in range(6):
        doc.add_paragraph()
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("课程交付文档（基于真实仓库实现与 FDE 合成训练资料编写）")
    run.font.size = Pt(9); run.font.color.rgb = RGBColor(0x80, 0x80, 0x80); _set_cn_font(run)

    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    # 版本记录
    _heading(doc, "版本记录", 1)
    _table(doc, [
        "| 版本 | 日期 | 说明 | 编写 |",
        "|---|---|---|---|",
        f"| {version} | {date.today().isoformat()} | 首版（DOCS 阶段交付） | 课程项目组 |",
    ])
    doc.add_paragraph()

    # 目录
    _heading(doc, "目录", 1)
    _add_toc(doc)
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    # 正文
    lines = open(md_path, encoding="utf-8").read().splitlines()
    i, in_code = 0, False
    code_buf = []
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            if in_code:
                p = doc.add_paragraph()
                run = p.add_run("\n".join(code_buf))
                run.font.name = "Consolas"; run.font.size = Pt(9)
                shd = OxmlElement("w:shd"); shd.set(qn("w:fill"), "F2F2F2")
                p._element.get_or_add_pPr().append(shd)
                code_buf = []
            in_code = not in_code
            i += 1; continue
        if in_code:
            code_buf.append(line); i += 1; continue

        if line.startswith("|") and i + 1 < len(lines) and lines[i + 1].strip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(lines[i]); i += 1
            _table(doc, rows); continue

        stripped = line.strip()
        if not stripped:
            i += 1; continue
        m = re.match(r"^(#{1,4})\s+(.*)", stripped)
        if m:
            _heading(doc, m.group(2).strip(), len(m.group(1)))
            i += 1; continue
        m = re.match(r"^!\[[^\]]*\]\((.+)\)", stripped)
        if m:
            img = os.path.normpath(os.path.join(base_dir, m.group(1)))
            try:
                doc.add_picture(img, width=Cm(15.5))
                doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            except Exception as e:
                doc.add_paragraph(f"[图片插入失败：{m.group(1)} ({e})]")
            i += 1; continue
        if stripped.startswith(">"):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.6)
            _inline(p, stripped.lstrip("> ").strip(), base_size=10.5)
            for run in p.runs:
                run.font.color.rgb = RGBColor(0x60, 0x60, 0x60)
            i += 1; continue
        m = re.match(r"^[-*]\s+(.*)", stripped)
        if m:
            p = doc.add_paragraph(style="List Bullet")
            _inline(p, m.group(1)); i += 1; continue
        m = re.match(r"^(\d+)[.、]\s+(.*)", stripped)
        if m:
            p = doc.add_paragraph(style="List Number")
            _inline(p, m.group(2)); i += 1; continue
        p = doc.add_paragraph()
        p.paragraph_format.first_line_indent = Cm(0.74)
        _inline(p, stripped)
        i += 1

    doc.save(out_path)
    print(f"OK {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("md"); ap.add_argument("docx")
    ap.add_argument("--title", required=True); ap.add_argument("--version", default="V1.0")
    ap.add_argument("--project", default="瀚川工业园能耗监测与异常定位智能体系统")
    ap.add_argument("--doc-code", required=True)
    a = ap.parse_args()
    convert(a.md, a.docx, a.title, a.version, a.project, a.doc_code)


if __name__ == "__main__":
    main()
