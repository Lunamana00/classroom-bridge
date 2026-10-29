"""Export the supplied CS231n PDFs and source code without executing notebooks.

The source directory is read-only. A manifest records every original file and
every exported notebook cell; outputs and markdown cells are never course data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from classroom.app import MAX_UPLOAD, parse_document
from pypdf import PdfReader

CODE_SUFFIXES = frozenset({".py", ".js", ".ts", ".jsx", ".tsx", ".c", ".cc", ".cpp", ".h", ".hpp", ".java", ".r", ".m", ".jl", ".sh", ".sql"})
BACKPROP = PurePosixPath("02_Review_and_Project/02_Backprop_Review_Session")
ALIASES = {
    (str(BACKPROP / "02_section_2_backprop.pdf"), None): "02_section_2_backprop.pdf",
    (str(BACKPROP / "01_backprop.ipynb"), 11): "01_backprop_cell_11.txt",
    (str(BACKPROP / "01_backprop.ipynb"), 18): "01_backprop_cell_18.txt",
}


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("에셋 파일은 폴더 내부 상대 경로여야 합니다.")
    return path


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _check_existing_output(output: Path) -> None:
    if not output.exists():
        return
    if output.is_symlink() or not output.is_dir():
        raise ValueError("출력 위치는 심볼릭 링크가 아닌 폴더여야 합니다.")
    files = {str(p.relative_to(output).as_posix()) for p in output.rglob("*") if p.is_file() or p.is_symlink()}
    if not files:
        return
    try:
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        expected = {str(_safe_relative(row["file"])) for row in manifest["sources"]} | {"manifest.json"}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("기존 출력이 관리되는 에셋 폴더인지 확인할 수 없습니다.") from exc
    if files != expected or any(p.is_symlink() for p in output.rglob("*")):
        raise ValueError("기존 출력에 관리 대상이 아닌 파일이 있어 교체하지 않았습니다.")


def _label(path: Path) -> str:
    return re.sub(r"^\d+_", "", path.name).replace("_", " ")


def _title(relative: PurePosixPath, cell_number: int | None = None) -> str:
    if relative == BACKPROP / "02_section_2_backprop.pdf":
        return "CS231n 2026 · Backpropagation 복습 PDF"
    if relative == BACKPROP / "01_backprop.ipynb" and cell_number in {11, 18}:
        code = "MLPClassifier.forward / backward" if cell_number == 11 else "numerical_gradient / grad_check"
        return f"CS231n 2026 · {code} · notebook 셀 {cell_number}"
    base = f"CS231n 2026 · {_label(Path(relative.parent))} · {relative.stem.replace('_', ' ')}"
    return f"{base} · notebook 셀 {cell_number}" if cell_number is not None else base


def _asset_name(relative: PurePosixPath, cell_number: int | None = None) -> str:
    alias = ALIASES.get((str(relative), cell_number))
    if alias:
        return alias
    if cell_number is not None:
        return str(PurePosixPath("code") / relative.with_suffix("") / f"cell_{cell_number:03}.txt")
    if relative.suffix.lower() == ".pdf":
        return str(PurePosixPath("pdf") / relative)
    return str(PurePosixPath("code_files") / (str(relative) + ".txt"))


def build_assets(source_root: str | Path, output_dir: str | Path) -> dict:
    source = Path(source_root).expanduser().resolve(strict=True)
    raw_output = Path(output_dir).expanduser()
    if raw_output.is_symlink():
        raise ValueError("출력 위치에 심볼릭 링크를 사용할 수 없습니다.")
    output = raw_output.resolve()
    if not source.is_dir() or _is_within(output, source) or _is_within(source, output):
        raise ValueError("원본 폴더와 출력 폴더는 서로 포함하지 않아야 합니다.")
    _check_existing_output(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.build-", dir=output.parent))
    replaced = None
    try:
        sources, originals, notebooks, skipped = [], [], [], []
        names = set()
        candidates = sorted((p for p in source.rglob("*") if p.suffix.lower() in CODE_SUFFIXES | {".pdf", ".ipynb"} and (p.is_file() or p.is_symlink())), key=lambda p: p.relative_to(source).as_posix())

        def write_asset(relative, data, original, *, cell_number=None, **metadata):
            filename = str(_safe_relative(_asset_name(relative, cell_number)))
            # Case-folding avoids colliding exports on case-insensitive volumes.
            if filename.casefold() in names:
                raise ValueError(f"에셋 파일 이름이 충돌합니다: {filename}")
            names.add(filename.casefold())
            target = staging / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if relative.suffix.lower() != ".pdf":
                parse_document(target.name, data)
            item = {"kind": "pdf" if relative.suffix.lower() == ".pdf" else "code", "file": filename,
                    "title": _title(relative, cell_number), "source_path": original["source_path"],
                    "source_relative_path": str(relative), "source_sha256": original["sha256"],
                    "sha256": sha256(data), "bytes": len(data), "upload_requires_admin": len(data) > MAX_UPLOAD,
                    **metadata}
            if item["upload_requires_admin"]:
                item["upload_note"] = "원본 보존. 웹 파일 업로드 제한을 초과하므로 관리용 stage로 등록합니다."
            if cell_number is not None:
                item["cell_number"] = cell_number
            sources.append(item)

        for path in candidates:
            resolved = path.resolve(strict=True)
            if not _is_within(resolved, source):
                raise ValueError(f"허용 폴더 밖을 가리키는 원본입니다: {path.relative_to(source)}")
            relative = PurePosixPath(path.relative_to(source).as_posix())
            data = path.read_bytes()
            original = {"source_path": str(path), "source_relative_path": str(relative), "sha256": sha256(data), "bytes": len(data), "kind": "notebook" if path.suffix.lower() == ".ipynb" else "pdf" if path.suffix.lower() == ".pdf" else "code"}
            originals.append(original)
            if original["kind"] == "notebook":
                notebook = json.loads(data.decode("utf-8-sig"))
                code_count, exported, empty = 0, 0, []
                for number, cell in enumerate(notebook["cells"], 1):
                    if cell.get("cell_type") != "code":
                        continue
                    code_count += 1
                    raw = cell.get("source", [])
                    if not isinstance(raw, (str, list)) or isinstance(raw, list) and any(not isinstance(part, str) for part in raw):
                        raise ValueError(f"코드 셀 source 형식이 올바르지 않습니다: {relative} 셀 {number}")
                    text = raw if isinstance(raw, str) else "".join(raw)
                    if not text.strip():
                        empty.append(number)
                        skipped.append({"source_relative_path": str(relative), "cell_number": number, "reason": "empty_or_whitespace_code_cell", "source_sha256": sha256(text.encode("utf-8"))})
                        continue
                    write_asset(relative, text.encode("utf-8"), original, cell_number=number, original_format="notebook_cell")
                    exported += 1
                notebooks.append({**original, "code_cell_count": code_count, "exported_code_cell_count": exported, "empty_code_cells": empty})
            elif original["kind"] == "pdf":
                text, pages = parse_document(path.name, data)
                write_asset(relative, data, original, original_format="pdf", page_count=len(PdfReader(path).pages), text_page_count=len(pages), text_chars=len(text))
            else:
                write_asset(relative, data, original, original_format="code_file")
        if not sources:
            raise ValueError("등록할 PDF 또는 코드가 없습니다.")
        manifest = {"format_version": 2, "course_title": "CS231n 2026", "allowed_root": str(source), "source_root": str(source),
                    "cell_numbering": "1-based index into notebook cells, including markdown cells",
                    "source_policy": "PDF originals and source code only; notebook outputs, markdown and external files excluded",
                    "source_scope": {"pdf_files": sum(row["kind"] == "pdf" for row in sources),
                                     "pdf_pages": sum(row.get("page_count", 0) for row in sources),
                                     "pdf_text_pages": sum(row.get("text_page_count", 0) for row in sources),
                                     "notebooks": len(notebooks), "code_cells": sum(row["exported_code_cell_count"] for row in notebooks),
                                     "empty_code_cells": len(skipped), "code_files": sum(row["kind"] == "code" for row in originals),
                                     "materials": len(sources), "original_source_files": len(originals),
                                     "original_bytes": sum(row["bytes"] for row in originals),
                                     "oversized_http_uploads": sum(row["upload_requires_admin"] for row in sources)},
                    "sources": sources, "original_files": originals, "notebooks": notebooks, "skipped_code_cells": skipped}
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for row in sources:
            if sha256((staging / row["file"]).read_bytes()) != row["sha256"]:
                raise RuntimeError("에셋 해시 검증에 실패했습니다.")
        # Do not remove existing data until the complete new export has passed.
        _check_existing_output(output)
        if output.exists():
            replaced = Path(tempfile.mkdtemp(prefix=f".{output.name}.previous-", dir=output.parent))
            replaced.rmdir()
            os.replace(output, replaced)
        try:
            os.replace(staging, output)
        except Exception:
            if replaced is not None:
                os.replace(replaced, output)
                replaced = None
            raise
        if replaced is not None:
            shutil.rmtree(replaced)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/Users/oyeonwu/Desktop/CS231n_2026"))
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "output/demo/assets")
    args = parser.parse_args(argv)
    try:
        manifest = build_assets(args.source, args.output)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, str(exc) + "\n")
    path = args.output.expanduser().resolve() / "manifest.json"
    print(json.dumps({"output": str(path.parent), "manifest_sha256": sha256(path.read_bytes()), **manifest["source_scope"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
