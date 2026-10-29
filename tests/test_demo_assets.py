import hashlib
import json
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, StreamObject

from deploy import build_demo_assets as builder


def pdf(path, text="Course source"):
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = StreamObject()
    stream.set_data(f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)


def notebook(path, cells):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cells": cells, "nbformat": 4}), encoding="utf-8")


def test_exports_all_originals_and_exact_code_cells_without_markdown_or_outputs(tmp_path):
    source, output = tmp_path / "source", tmp_path / "assets"
    pdf(source / "Lectures" / "lecture.pdf")
    raw = ["raise RuntimeError('must never execute')\n", "x = '원문'\n"]
    nb = source / "Review" / "exercise.ipynb"
    notebook(nb, [{"cell_type": "markdown", "source": ["NOT COURSE DATA"]},
                  {"cell_type": "code", "source": raw, "outputs": [{"text": "SECRET OUTPUT"}]},
                  {"cell_type": "code", "source": "  \n"},
                  {"cell_type": "code", "source": "print(x)"}])
    (source / "README.md").write_text("DO NOT IMPORT")
    (source / "Review" / "script.py").write_bytes(b"print('source file')\n")
    manifest = builder.build_assets(source, output)
    assert manifest["source_scope"] == {"pdf_files": 1, "pdf_pages": 1, "pdf_text_pages": 1, "notebooks": 1, "code_cells": 2, "empty_code_cells": 1, "code_files": 1, "materials": 4, "original_source_files": 3, "original_bytes": sum(p.stat().st_size for p in [source / "Lectures/lecture.pdf", nb, source / "Review/script.py"]), "oversized_http_uploads": 0}
    cell = next(row for row in manifest["sources"] if row.get("cell_number") == 2)
    assert (output / cell["file"]).read_bytes() == "".join(raw).encode("utf-8")
    assert cell["source_sha256"] == hashlib.sha256(nb.read_bytes()).hexdigest()
    assert manifest["skipped_code_cells"][0]["cell_number"] == 3
    assert not any(p.suffix in {".ipynb", ".md"} for p in output.rglob("*"))
    assert b"SECRET OUTPUT" not in b"".join(p.read_bytes() for p in output.rglob("*") if p.is_file())
    for row in manifest["sources"]:
        assert hashlib.sha256((output / row["file"]).read_bytes()).hexdigest() == row["sha256"]


def test_backprop_aliases_and_repeated_basenames_do_not_collide(tmp_path):
    source, output = tmp_path / "source", tmp_path / "assets"
    parent = source / str(builder.BACKPROP)
    pdf(parent / "02_section_2_backprop.pdf")
    pdf(source / "other" / "02_section_2_backprop.pdf", "Other supplied PDF")
    cells = [{"cell_type": "code", "source": f"cell_{number} = {number}\n"} for number in range(1, 19)]
    notebook(parent / "01_backprop.ipynb", cells)
    manifest = builder.build_assets(source, output)
    names = [row["file"] for row in manifest["sources"]]
    assert len(names) == len(set(names)) == 20
    assert {"02_section_2_backprop.pdf", "01_backprop_cell_11.txt", "01_backprop_cell_18.txt"} <= set(names)
    assert (output / "01_backprop_cell_11.txt").read_text() == cells[10]["source"]
    assert (output / "02_section_2_backprop.pdf").read_bytes() == (parent / "02_section_2_backprop.pdf").read_bytes()


def test_outside_symlink_is_rejected_and_original_output_survives(tmp_path):
    source, output = tmp_path / "source", tmp_path / "assets"
    pdf(source / "lecture.pdf")
    builder.build_assets(source, output)
    before = (output / "manifest.json").read_bytes()
    outside = tmp_path / "outside.pdf"
    pdf(outside)
    (source / "outside-link.pdf").symlink_to(outside)
    with pytest.raises(ValueError, match="폴더 밖"):
        builder.build_assets(source, output)
    assert (output / "manifest.json").read_bytes() == before
    assert not list(tmp_path.glob(".assets.build-*"))


def test_rebuild_is_reproducible_and_never_removes_unmanaged_files(tmp_path):
    source, output = tmp_path / "source", tmp_path / "assets"
    pdf(source / "lecture.pdf")
    builder.build_assets(source, output)
    before = (output / "manifest.json").read_bytes()
    builder.build_assets(source, output)
    assert (output / "manifest.json").read_bytes() == before
    (output / "personal-notes.txt").write_text("keep this")
    with pytest.raises(ValueError, match="관리 대상이 아닌"):
        builder.build_assets(source, output)
    assert (output / "personal-notes.txt").read_text() == "keep this"
    assert not list(tmp_path.glob(".assets.build-*"))


def test_oversize_http_upload_keeps_the_original_and_marks_admin_registration(tmp_path, monkeypatch):
    source, output = tmp_path / "source", tmp_path / "assets"
    original = source / "lecture.pdf"
    pdf(original)
    monkeypatch.setattr(builder, "MAX_UPLOAD", original.stat().st_size - 1)
    manifest = builder.build_assets(source, output)
    assert manifest["source_scope"]["oversized_http_uploads"] == 1
    entry = manifest["sources"][0]
    assert entry["upload_requires_admin"]
    assert (output / entry["file"]).read_bytes() == original.read_bytes()


def test_invalid_pdf_and_source_output_overlap_are_rejected_before_replacement(tmp_path):
    source, output = tmp_path / "source", tmp_path / "assets"
    pdf(source / "lecture.pdf")
    builder.build_assets(source, output)
    before = (output / "manifest.json").read_bytes()
    (source / "broken.pdf").write_bytes(b"not a PDF")
    with pytest.raises(ValueError, match="PDF"):
        builder.build_assets(source, output)
    assert (output / "manifest.json").read_bytes() == before
    with pytest.raises(ValueError, match="포함하지"):
        builder.build_assets(source, source / "generated")
