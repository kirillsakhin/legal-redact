# DOCX review fixture (synthetic)

`contract.docx` and `contract.pdf` are a matched pair used only by
`tests/test_docx_review.py`. **Every name, address and number in them is
invented**; there is no real client data.

- `contract.docx` is a two-page synthetic consulting agreement generated with
  `python-docx` (plus one injected floating text box), with a header, a footer,
  a table and ordinary contract filler text.
- `contract.pdf` is that same document rendered **once** with Microsoft Word
  (`ExportAsFixedFormat`) on Windows, so the alignment tests can run on CI where
  neither Word nor LibreOffice is installed: the tests read the committed PDF.

Regenerate with `make_fixture.py` on a machine that has Word (or edit it to use
LibreOffice). Regenerate both files together — an old PDF rendered from a
different .docx would invalidate the alignment tests. The generator is
development tooling only; the test suite never runs it.
