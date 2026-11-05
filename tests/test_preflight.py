import pymupdf

from legal_redact.cli import main
from legal_redact.models import Severity
from legal_redact.preflight import blocked, preflight


def codes(path):
    return {f.code: f for f in preflight(pymupdf.open(path))}


def test_clean_text_pdf_is_supported(plain_pdf):
    fs = preflight(pymupdf.open(plain_pdf))
    assert not blocked(fs)
    assert not [f for f in fs if f.severity is Severity.REVIEW]


def test_image_only_pdf_is_refused(tmp_path):
    src = pymupdf.open()
    pg = src.new_page()
    pg.insert_text((72, 72), "Alice Example", fontsize=20)
    pix = pg.get_pixmap()
    img = pymupdf.open()
    ip = img.new_page(width=pg.rect.width, height=pg.rect.height)
    ip.insert_image(ip.rect, pixmap=pix)
    out = tmp_path / "scan.pdf"
    img.save(out)
    c = codes(str(out))
    assert "PF003" in c and blocked(list(c.values()))


def test_encrypted_pdf_is_refused(tmp_path, plain_pdf):
    out = tmp_path / "enc.pdf"
    d = pymupdf.open(plain_pdf)
    d.save(out, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")
    assert "PF001" in codes(str(out))


def test_embedded_file_is_refused(tmp_path, plain_pdf):
    d = pymupdf.open(plain_pdf)
    d.embfile_add("secret.txt", b"Alice Example")
    out = tmp_path / "emb.pdf"
    d.save(out)
    assert "PF009" in codes(str(out))


def test_annotation_needs_review(tmp_path, plain_pdf):
    d = pymupdf.open(plain_pdf)
    d[0].add_text_annot((300, 300), "note about Alice Example")
    out = tmp_path / "ann.pdf"
    d.save(out)
    f = codes(str(out))["PF007"]
    assert f.severity is Severity.REVIEW
    assert "Alice" not in f.message          # findings never leak document text


def test_cli_inspect_output_has_no_document_text(plain_pdf, capsys):
    rc = main(["inspect", plain_pdf])
    out = capsys.readouterr().out
    assert rc == 0 and "pages=1" in out
    assert "Alice" not in out and "example.test" not in out


def test_cli_rejects_non_pdf(tmp_path, capsys):
    p = tmp_path / "x.pdf"
    p.write_text("not a pdf")
    assert main(["inspect", str(p)]) == 2
