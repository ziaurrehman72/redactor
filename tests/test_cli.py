"""Tests for redactor.cli.

Most tests replace the real redactor with a fast fake so they exercise only
input discovery, naming, skipping, error handling and exit codes. A couple of
end-to-end tests at the bottom use the real thing.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pymupdf
import pytest

from redactor import __version__, cli
from redactor.core import RedactionError, RedactionResult


# --------------------------------------------------------------------------- #
# helpers / fixtures
# --------------------------------------------------------------------------- #
class Recorder:
    """Stand-in for redact_report: writes a stub file, remembers the calls."""

    def __init__(self):
        self.calls: list[tuple[Path, Path, str]] = []
        self.fail_on: dict[str, Exception] = {}
        self.warnings: list[str] = []

    def __call__(self, src, dst, password=""):
        self.calls.append((Path(src), Path(dst), password))
        if Path(src).name in self.fail_on:
            raise self.fail_on[Path(src).name]
        Path(dst).write_bytes(b"%PDF-fake")
        return RedactionResult(Path(src), Path(dst), 3, None, list(self.warnings))

    @property
    def srcs(self):
        return [c[0].name for c in self.calls]

    @property
    def dsts(self):
        return [c[1] for c in self.calls]


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(cli, "redact_report", r)
    monkeypatch.delenv("REDACTOR_PASSWORD", raising=False)
    return r


@pytest.fixture
def tree(tmp_path) -> Path:
    """in/a.pdf, in/B.PDF, in/notes.txt, in/sub/c.pdf, in/sub/deep/d.pdf"""
    root = tmp_path / "in"
    for rel in ["a.pdf", "B.PDF", "notes.txt", "sub/c.pdf", "sub/deep/d.pdf"]:
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"%PDF-1.4 stub")
    return root


def run(*argv, capsys):
    code = cli.main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


# --------------------------------------------------------------------------- #
# Single / multiple files
# --------------------------------------------------------------------------- #
def test_single_file(tree, tmp_path, rec, capsys):
    out = tmp_path / "out"
    code, stdout, _ = run(tree / "a.pdf", "-o", out, capsys=capsys)
    assert code == 0
    assert rec.dsts == [out / "a_redacted.pdf"]
    assert (out / "a_redacted.pdf").exists()
    assert "[1/1] OK" in stdout and "3 pages" in stdout
    assert "done: 1 redacted, 0 skipped, 0 failed" in stdout


def test_multiple_files(tree, tmp_path, rec, capsys):
    out = tmp_path / "out"
    code, stdout, _ = run(tree / "a.pdf", tree / "B.PDF", tree / "sub/c.pdf", "-o", out, capsys=capsys)
    assert code == 0
    assert sorted(p.name for p in out.iterdir()) == ["B_redacted.pdf", "a_redacted.pdf", "c_redacted.pdf"]
    assert "[3/3]" in stdout and "3 redacted" in stdout


def test_default_output_dir_is_output_in_cwd(tree, tmp_path, rec, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code, *_ = run(tree / "a.pdf", capsys=capsys)
    assert code == 0 and (tmp_path / "output" / "a_redacted.pdf").exists()


def test_custom_suffix(tree, tmp_path, rec, capsys):
    run(tree / "a.pdf", "-o", tmp_path / "o", "-s", "_clean", capsys=capsys)
    assert rec.dsts == [tmp_path / "o" / "a_clean.pdf"]


def test_creates_output_dir(tree, tmp_path, rec, capsys):
    out = tmp_path / "x" / "y" / "z"
    run(tree / "a.pdf", "-o", out, capsys=capsys)
    assert out.is_dir()


def test_same_file_twice_is_processed_once(tree, tmp_path, rec, capsys):
    code, *_ = run(tree / "a.pdf", tree / "a.pdf", tree / ".." / "in" / "a.pdf",
                   "-o", tmp_path / "o", capsys=capsys)
    assert code == 0 and rec.srcs == ["a.pdf"]


# --------------------------------------------------------------------------- #
# Directories and globs
# --------------------------------------------------------------------------- #
def test_directory_is_not_recursive_by_default(tree, tmp_path, rec, capsys):
    code, *_ = run(tree, "-o", tmp_path / "o", capsys=capsys)
    assert code == 0
    assert sorted(rec.srcs) == ["B.PDF", "a.pdf"]  # case-insensitive ext, no sub/, no .txt


def test_recursive_directory_preserves_structure(tree, tmp_path, rec, capsys):
    out = tmp_path / "o"
    code, *_ = run(tree, "-r", "-o", out, capsys=capsys)
    assert code == 0
    assert sorted(str(p.relative_to(out)) for p in out.rglob("*.pdf")) == [
        "B_redacted.pdf", "a_redacted.pdf",
        "sub/c_redacted.pdf", "sub/deep/d_redacted.pdf",
    ]


def test_recursive_same_names_in_different_folders_do_not_collide(tmp_path, rec, capsys):
    root = tmp_path / "in"
    for d in ("x", "y"):
        (root / d).mkdir(parents=True)
        (root / d / "r.pdf").write_bytes(b"%PDF")
    code, *_ = run(root, "-r", "-o", tmp_path / "o", capsys=capsys)
    assert code == 0 and len(rec.calls) == 2
    assert {c[1].parent.name for c in rec.calls} == {"x", "y"}


def test_empty_directory(tmp_path, rec, capsys):
    (tmp_path / "empty").mkdir()
    code, _, err = run(tmp_path / "empty", capsys=capsys)
    assert code == 2 and "no PDF files found (use -r to recurse)" in err
    assert "nothing to redact" in err and rec.calls == []


def test_empty_directory_recursive_message_has_no_hint(tmp_path, rec, capsys):
    (tmp_path / "empty").mkdir()
    _, _, err = run(tmp_path / "empty", "-r", capsys=capsys)
    assert "no PDF files found" in err and "use -r" not in err


def test_glob_is_expanded_by_the_tool(tree, tmp_path, rec, capsys):
    code, *_ = run(str(tree / "*.pdf"), "-o", tmp_path / "o", capsys=capsys)
    assert code == 0 and rec.srcs == ["a.pdf"]  # case-sensitive on Linux; B.PDF not matched


def test_recursive_glob(tree, tmp_path, rec, capsys):
    run(str(tree / "**" / "*.pdf"), "-o", tmp_path / "o", capsys=capsys)
    assert sorted(rec.srcs) == ["a.pdf", "c.pdf", "d.pdf"]


def test_glob_without_matches(tree, rec, capsys):
    code, _, err = run(str(tree / "*.nothing"), capsys=capsys)
    assert code == 2 and "no files match" in err


def test_mixed_files_dirs_and_globs(tree, tmp_path, rec, capsys):
    code, *_ = run(tree / "a.pdf", tree / "sub", str(tree / "sub" / "deep" / "*.pdf"),
                   "-o", tmp_path / "o", capsys=capsys)
    assert code == 0 and sorted(rec.srcs) == ["a.pdf", "c.pdf", "d.pdf"]


# --------------------------------------------------------------------------- #
# Bad inputs
# --------------------------------------------------------------------------- #
def test_missing_file(tmp_path, rec, capsys):
    code, _, err = run(tmp_path / "nope.pdf", capsys=capsys)
    assert code == 2 and "no such file or directory" in err and rec.calls == []


def test_non_pdf_file(tree, rec, capsys):
    code, _, err = run(tree / "notes.txt", capsys=capsys)
    assert code == 2 and "not a .pdf file" in err and rec.calls == []


def test_one_bad_input_does_not_stop_the_good_ones(tree, tmp_path, rec, capsys):
    code, stdout, err = run(tmp_path / "nope.pdf", tree / "a.pdf", "-o", tmp_path / "o", capsys=capsys)
    assert code == 1  # a problem counts as a failure
    assert rec.srcs == ["a.pdf"]
    assert "no such file" in err
    assert "1 redacted, 0 skipped, 1 failed" in stdout


def test_no_arguments_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main([])
    assert e.value.code == 2
    assert "INPUT" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Skip / force / collisions / output-dir handling
# --------------------------------------------------------------------------- #
def test_existing_output_is_skipped_by_default(tree, tmp_path, rec, capsys):
    out = tmp_path / "o"
    out.mkdir()
    (out / "a_redacted.pdf").write_bytes(b"precious")
    code, stdout, _ = run(tree / "a.pdf", "-o", out, capsys=capsys)
    assert code == 0  # skipping is not a failure
    assert rec.calls == []
    assert (out / "a_redacted.pdf").read_bytes() == b"precious"
    assert "SKIP" in stdout and "use --force" in stdout and "1 skipped" in stdout


def test_force_overwrites(tree, tmp_path, rec, capsys):
    out = tmp_path / "o"
    out.mkdir()
    (out / "a_redacted.pdf").write_bytes(b"old")
    code, *_ = run(tree / "a.pdf", "-o", out, "--force", capsys=capsys)
    assert code == 0 and rec.srcs == ["a.pdf"]
    assert (out / "a_redacted.pdf").read_bytes() == b"%PDF-fake"


def test_output_name_collision_is_reported_not_overwritten(tree, tmp_path, rec, capsys):
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.pdf").write_bytes(b"%PDF")
    code, stdout, err = run(tree / "a.pdf", other / "a.pdf", "-o", tmp_path / "o", capsys=capsys)
    assert code == 1
    assert rec.srcs == ["a.pdf"] and rec.calls[0][0].parent == tree  # first one wins
    assert "already used by" in err and "1 failed" in stdout


def test_files_inside_output_dir_are_never_reprocessed(tree, rec, capsys):
    out = tree / "out"  # output dir lives inside the scanned dir
    out.mkdir()
    (out / "old_redacted.pdf").write_bytes(b"%PDF")
    code, *_ = run(tree, "-r", "-o", out, capsys=capsys)
    assert code == 0 and "old_redacted.pdf" not in rec.srcs
    assert sorted(rec.srcs) == ["B.PDF", "a.pdf", "c.pdf", "d.pdf"]


def test_only_files_in_output_dir_gives_helpful_note(tree, rec, capsys):
    code, _, err = run(tree / "a.pdf", "-o", tree, capsys=capsys)
    assert code == 2
    assert "inside the output directory" in err and "nothing to redact" in err
    assert rec.calls == []


def test_output_dir_that_is_a_file_fails_that_file(tree, tmp_path, rec, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")
    code, stdout, err = run(tree / "a.pdf", "-o", blocker, capsys=capsys)
    assert code == 1 and "FAIL" in err and "1 failed" in stdout


# --------------------------------------------------------------------------- #
# Errors during redaction, warnings, password, verbosity
# --------------------------------------------------------------------------- #
def test_failure_in_one_file_does_not_stop_the_batch(tree, tmp_path, rec, capsys):
    rec.fail_on["a.pdf"] = RedactionError("kaboom")
    code, stdout, err = run(tree / "a.pdf", tree / "B.PDF", tree / "sub/c.pdf",
                            "-o", tmp_path / "o", capsys=capsys)
    assert code == 1
    assert rec.srcs == ["a.pdf", "B.PDF", "c.pdf"]  # all attempted
    assert "[1/3] FAIL" in err and "kaboom" in err
    assert "2 redacted, 0 skipped, 1 failed" in stdout


def test_oserror_is_handled_like_redaction_error(tree, tmp_path, rec, capsys):
    rec.fail_on["a.pdf"] = PermissionError("denied")
    code, _, err = run(tree / "a.pdf", "-o", tmp_path / "o", capsys=capsys)
    assert code == 1 and "denied" in err


def test_unexpected_exception_is_not_swallowed(tree, tmp_path, rec, capsys):
    rec.fail_on["a.pdf"] = ZeroDivisionError("bug")
    with pytest.raises(ZeroDivisionError):
        cli.main([str(tree / "a.pdf"), "-o", str(tmp_path / "o")])


def test_warnings_go_to_stderr_and_do_not_fail(tree, tmp_path, rec, capsys):
    rec.warnings = ["page 2: no collection date found"]
    code, stdout, err = run(tree / "a.pdf", "-o", tmp_path / "o", capsys=capsys)
    assert code == 0
    assert "warn" in err and "page 2: no collection date found" in err
    assert "page 2" not in stdout


def test_password_option(tree, tmp_path, rec, capsys):
    run(tree / "a.pdf", "-o", tmp_path / "o", "-p", "hunter2", capsys=capsys)
    assert rec.calls[0][2] == "hunter2"


def test_password_from_environment(tree, tmp_path, rec, monkeypatch, capsys):
    monkeypatch.setenv("REDACTOR_PASSWORD", "fromenv")
    run(tree / "a.pdf", "-o", tmp_path / "o", capsys=capsys)
    assert rec.calls[0][2] == "fromenv"


def test_password_option_beats_environment(tree, tmp_path, rec, monkeypatch, capsys):
    monkeypatch.setenv("REDACTOR_PASSWORD", "fromenv")
    run(tree / "a.pdf", "-o", tmp_path / "o", "--password", "explicit", capsys=capsys)
    assert rec.calls[0][2] == "explicit"


def test_default_password_is_empty(tree, tmp_path, rec, capsys):
    run(tree / "a.pdf", "-o", tmp_path / "o", capsys=capsys)
    assert rec.calls[0][2] == ""


def test_quiet_hides_progress_but_not_errors(tree, tmp_path, rec, capsys):
    rec.fail_on["B.PDF"] = RedactionError("bad one")
    code, stdout, err = run(tree / "a.pdf", tree / "B.PDF", "-o", tmp_path / "o", "-q", capsys=capsys)
    assert code == 1 and stdout == ""
    assert "FAIL" in err and "bad one" in err


def test_quiet_success_prints_nothing(tree, tmp_path, rec, capsys):
    code, stdout, err = run(tree / "a.pdf", "-o", tmp_path / "o", "-q", capsys=capsys)
    assert (code, stdout, err) == (0, "", "")


def test_keyboard_interrupt_exits_130(tree, tmp_path, rec, capsys):
    rec.fail_on["a.pdf"] = KeyboardInterrupt()
    code, _, err = run(tree / "a.pdf", tree / "B.PDF", "-o", tmp_path / "o", capsys=capsys)
    assert code == 130 and "interrupted" in err
    assert rec.srcs == ["a.pdf"]  # stopped immediately


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_help_mentions_examples(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["-h"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "--output-dir" in out and "--recursive" in out and "examples:" in out


def test_module_entry_point(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["redactor", "--version"])
    with pytest.raises(SystemExit) as e:
        runpy.run_module("redactor", run_name="__main__")
    assert e.value.code == 0
    assert __version__ in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# End-to-end with the real redactor
# --------------------------------------------------------------------------- #
def test_end_to_end_real_files(minimal_report, tmp_path, capsys):
    second = tmp_path / "second.pdf"
    second.write_bytes(minimal_report.read_bytes())
    out = tmp_path / "o"
    code = cli.main([str(minimal_report), str(second), "-o", str(out)])
    assert code == 0
    for name in ("mini_redacted.pdf", "second_redacted.pdf"):
        with pymupdf.open(out / name) as d:
            text = d[0].get_text()
        assert "Jane Roe" not in text and "Collected : 1/2/2024 09:30:00AM" in text
    assert "2 redacted" in capsys.readouterr().out


def test_end_to_end_bad_pdf_among_good(minimal_report, tmp_path, capsys):
    bad = tmp_path / "bad.pdf"
    bad.write_text("not a pdf")
    out = tmp_path / "o"
    code = cli.main([str(bad), str(minimal_report), "-o", str(out)])
    assert code == 1
    assert (out / "mini_redacted.pdf").exists() and not (out / "bad_redacted.pdf").exists()
    assert not list(out.glob(".*tmp"))
    err = capsys.readouterr().err
    assert "bad.pdf" in err and "not a readable PDF" in err


def test_end_to_end_encrypted_input(minimal_report, tmp_path, capsys):
    locked = tmp_path / "locked.pdf"
    with pymupdf.open(minimal_report) as d:
        d.save(locked, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="own")
    out = tmp_path / "o"
    assert cli.main([str(locked), "-o", str(out)]) == 1
    assert "password" in capsys.readouterr().err
    assert cli.main([str(locked), "-o", str(out), "-p", "pw"]) == 0
    assert (out / "locked_redacted.pdf").exists()
