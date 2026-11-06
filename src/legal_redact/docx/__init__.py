"""DOCX (OOXML WordprocessingML) support: extraction, pre-flight, writer, verifier.

Scope for 0.3.0 is deliberately narrow and fail-closed:

- read: ``word/document.xml``, headers, footers, footnotes, endnotes and text
  boxes inside any of them;
- refuse: encrypted containers, binary .doc, macro-enabled files, tracked
  changes, embedded OLE/package objects, unknown parts, text outside the
  supported containers;
- remove: the accepted characters (replaced with U+2588 in the run that held
  them), comments, tracked-change residue, hidden text carriers, metadata,
  custom XML and page thumbnails;
- verify: with stdlib readers that do not share the writer's lxml stack.

Known gaps are listed in ``docs/DOCX.md`` and in every verification report.
"""
from .package import (FileKind, DocxError, file_kind, read_package,
                      read_package_bytes)
from .extract import DocxText, TextContainer, TextRun, extract_text, to_page_texts
from .preflight import blocked, preflight_docx
from .write import redact_docx
from .verify import DOCX_LIMITS, verify_docx
from .pipeline import (export_docx, load_decisions_docx, suggest_docx,
                       summary_docx)

__all__ = [
    "FileKind", "DocxError", "file_kind", "read_package", "read_package_bytes",
    "DocxText", "TextContainer", "TextRun", "extract_text", "to_page_texts",
    "blocked", "preflight_docx", "redact_docx", "DOCX_LIMITS", "verify_docx",
    "export_docx", "load_decisions_docx", "suggest_docx", "summary_docx",
]
