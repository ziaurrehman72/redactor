"""Command-line interface: redact one or many lab-report PDFs."""
from __future__ import annotations

import argparse
import glob
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .core import RedactionError, redact_report

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


@dataclass
class Job:
    src: Path
    dst: Path


# --------------------------------------------------------------------------- #
# Input discovery
# --------------------------------------------------------------------------- #
def _is_pdf(p: Path) -> bool:
    return p.suffix.lower() == ".pdf"


def collect_jobs(inputs: list[str], out_dir: Path, suffix: str, recursive: bool):
    """Expand files / directories / globs into (jobs, problems, ignored).

    * a file          -> <out_dir>/<name><suffix>.pdf
    * a directory     -> every PDF inside it, keeping its sub-folder layout
                         under <out_dir> (so same-named files do not collide)
    * a glob pattern  -> expanded here too, for shells that do not (Windows)
    Files already inside `out_dir` are never picked up again.
    """
    jobs: list[Job] = []
    problems: list[str] = []
    ignored: list[Path] = []  # inputs that live inside the output dir
    seen_src: set[Path] = set()
    seen_dst: dict[Path, Path] = {}
    out_root = out_dir.resolve()

    def add(src: Path, rel_parent: Path) -> None:
        real = src.resolve()
        if real in seen_src:
            return
        seen_src.add(real)
        if real.is_relative_to(out_root):
            ignored.append(src)  # never re-process our own output
            return
        dst = out_dir / rel_parent / f"{src.stem}{suffix}.pdf"
        key = dst.resolve()
        if key in seen_dst:
            problems.append(
                f"{src}: output name {dst} already used by {seen_dst[key]} "
                "(pass the files in separate runs or rename one)"
            )
            return
        seen_dst[key] = src
        jobs.append(Job(src, dst))

    for raw in inputs:
        path = Path(raw)
        if not path.exists() and glob.has_magic(raw):
            matches = sorted(glob.glob(raw, recursive=True))
            if not matches:
                problems.append(f"{raw}: no files match")
            for m in matches:
                _expand(Path(m), recursive, add, problems)
        else:
            _expand(path, recursive, add, problems)
    return jobs, problems, ignored


def _expand(path: Path, recursive: bool, add, problems: list[str]) -> None:
    if path.is_dir():
        pattern = "**/*" if recursive else "*"
        found = sorted(p for p in path.glob(pattern) if p.is_file() and _is_pdf(p))
        if not found:
            problems.append(f"{path}: no PDF files found" + ("" if recursive else " (use -r to recurse)"))
        for f in found:
            add(f, f.parent.relative_to(path))
    elif path.is_file():
        if _is_pdf(path):
            add(path, Path())
        else:
            problems.append(f"{path}: not a .pdf file")
    else:
        problems.append(f"{path}: no such file or directory")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="redactor",
        description=(
            "Remove personal information (patient block, headers, footers, barcodes, "
            "signatures) from lab-report PDFs. The report body and the collection "
            "date/time are kept."
        ),
        epilog=(
            "examples:\n"
            "  redactor report.pdf\n"
            "  redactor a.pdf b.pdf c.pdf -o clean/\n"
            "  redactor reports/ -r -o clean/        # whole folder tree\n"
            "  redactor 'scans/*.pdf' --force\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("inputs", nargs="+", metavar="INPUT",
                   help="PDF files, directories containing PDFs, or glob patterns")
    p.add_argument("-o", "--output-dir", type=Path, default=Path("output"), metavar="DIR",
                   help="where redacted files are written (default: ./output)")
    p.add_argument("-s", "--suffix", default="_redacted",
                   help="added to each output file name (default: _redacted)")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="search directories recursively")
    p.add_argument("-f", "--force", action="store_true",
                   help="overwrite existing output files (default: skip them)")
    p.add_argument("-p", "--password", default=os.environ.get("REDACTOR_PASSWORD", ""),
                   help="password for encrypted inputs (or set REDACTOR_PASSWORD)")
    p.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def info(msg: str) -> None:
        if not args.quiet:
            print(msg)

    def err(msg: str) -> None:
        print(msg, file=sys.stderr)

    jobs, problems, ignored = collect_jobs(args.inputs, args.output_dir, args.suffix, args.recursive)
    for msg in problems:
        err(f"error: {msg}")
    if not jobs:
        if ignored:
            err(f"note: {len(ignored)} file(s) skipped because they are inside the "
                f"output directory ({args.output_dir}); choose a different -o")
        err("error: nothing to redact")
        return EXIT_USAGE

    ok = skipped = 0
    failed = len(problems)
    try:
        for n, job in enumerate(jobs, 1):
            tag = f"[{n}/{len(jobs)}]"
            if job.dst.exists() and not args.force:
                skipped += 1
                info(f"{tag} SKIP  {job.src}  ({job.dst} exists; use --force)")
                continue
            try:
                job.dst.parent.mkdir(parents=True, exist_ok=True)
                res = redact_report(job.src, job.dst, args.password)
            except (RedactionError, OSError) as exc:
                failed += 1
                err(f"{tag} FAIL  {job.src}: {exc}")
                continue
            ok += 1
            info(f"{tag} OK    {job.src} -> {job.dst}  ({res.pages} pages)")
            for w in res.warnings:
                err(f"{tag} warn  {job.src}: {w}")
    except KeyboardInterrupt:
        err("\ninterrupted")
        return 130

    info(f"\ndone: {ok} redacted, {skipped} skipped, {failed} failed")
    return EXIT_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
