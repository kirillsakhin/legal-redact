"""Optional page renderer for .docx review.

The review page is a *view*: the source of truth stays the .docx. To show the
document as Word lays it out, the tool asks an installed office suite to convert
the file to PDF once, and the review page serves images of those pages. The PDF
is written to a temporary directory, never to the user's output path, and is
deleted when the review session ends.

Renderers, in preference order:

1. **Microsoft Word** on Windows, driven through COM from PowerShell (no
   ``pywin32`` dependency). Word is opened invisible, the document is opened
   read-only with macros force-disabled and link updates off, exported with
   ``ExportAsFixedFormat``, and then closed and quit. If COM attached to an
   already-running Word instance (no new ``WINWORD.EXE`` appeared) the script
   does not quit it, so a user's open Word is never closed.
2. **LibreOffice** (``soffice --headless``) with a unique user-profile directory,
   so concurrent runs cannot lock each other's profile.

There is no Python renderer: no new mandatory dependency is added. If neither
program is present, the review page falls back to text review; that is a
first-class mode, not an error.

Detection never installs or configures anything:
``LEGAL_REDACT_RENDERER=word|libreoffice|none`` overrides the choice,
``LEGAL_REDACT_SOFFICE`` points at a particular ``soffice`` binary, and
``LEGAL_REDACT_RENDER_TIMEOUT`` sets the timeout in seconds.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_TIMEOUT = 120.0
_RENDERER_ENV = "LEGAL_REDACT_RENDERER"
_SOFFICE_ENV = "LEGAL_REDACT_SOFFICE"
_TIMEOUT_ENV = "LEGAL_REDACT_RENDER_TIMEOUT"

_WORD_SCRIPT = r"""
$ErrorActionPreference = "Stop"
$src = $env:LEGAL_REDACT_SRC
$out = $env:LEGAL_REDACT_OUT
$before = @(Get-Process WINWORD -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$word = $null
$doc = $null
$owned = $false
try {
    $word = New-Object -ComObject Word.Application
    $after = @(Get-Process WINWORD -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
    $owned = @($after | Where-Object { $before -notcontains $_ }).Count -gt 0
    try { $word.Visible = $false } catch {}
    try { $word.DisplayAlerts = 0 } catch {}
    try { $word.AutomationSecurity = 3 } catch {}   # msoAutomationSecurityForceDisable
    try { $word.Options.UpdateLinksAtOpen = $false } catch {}
    $doc = $word.Documents.Open($src, $false, $true, $false)
    if ($null -eq $doc) { throw "Word could not open the document" }
    $doc.ExportAsFixedFormat($out, 17)              # wdExportFormatPDF
    if (-not (Test-Path -LiteralPath $out)) { throw "Word produced no PDF" }
    Write-Output "OK"
}
finally {
    if ($null -ne $doc) { try { $doc.Close($false) } catch {} }
    if ($null -ne $word) {
        if ($owned) { try { $word.Quit() } catch {} }
        try { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($word) | Out-Null } catch {}
        [System.GC]::Collect()
        [System.GC]::WaitForPendingFinalizers()
    }
}
"""


class RenderError(Exception):
    """A renderer was found but the document could not be rendered."""


class RendererUnavailable(RenderError):
    """No Word or LibreOffice installation is available."""


class RenderTimeout(RenderError):
    """The renderer did not finish within the timeout."""


# ---- detection -------------------------------------------------------------
def _word_available() -> bool:
    """True when Word's COM class is registered (Windows only). Never raises."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        for hive, path in ((winreg.HKEY_CLASSES_ROOT, r"Word.Application\CLSID"),
                           (winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Classes\Word.Application\CLSID")):
            try:
                with winreg.OpenKey(hive, path):
                    return True
            except OSError:
                continue
    except Exception:                         # a broken registry is "no renderer"
        return False
    return False


def _libreoffice_path() -> str | None:
    """Path to a LibreOffice executable, or None. Never installs anything, never raises."""
    try:
        override = os.environ.get(_SOFFICE_ENV, "").strip()
        if override:
            return override if Path(override).is_file() else None
        for name in ("soffice", "libreoffice"):
            found = shutil.which(name)
            if found:
                return found
        candidates = [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
            "/usr/bin/soffice",
            "/usr/lib/libreoffice/program/soffice",
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        ]
        for candidate in candidates:
            if Path(candidate).is_file():
                return candidate
    except Exception:                         # an unreadable PATH is "no renderer"
        return None
    return None


def detect_renderer() -> str | None:
    """Return "word", "libreoffice" or None. Cheap and side-effect free."""
    requested = os.environ.get(_RENDERER_ENV, "").strip().lower()
    if requested == "none":
        return None
    if requested == "word":
        return "word" if _word_available() else None
    if requested == "libreoffice":
        return "libreoffice" if _libreoffice_path() else None
    if _word_available():
        return "word"
    if _libreoffice_path():
        return "libreoffice"
    return None


def renderer_label(kind: str | None) -> str:
    """Short human label used by `inspect` and the review page."""
    if kind == "word":
        return "rendered (word)"
    if kind == "libreoffice":
        return "rendered (libreoffice)"
    return "text"


def renderer_name(kind: str | None) -> str:
    """The program name, for prose in the review page."""
    if kind == "word":
        return "Microsoft Word"
    if kind == "libreoffice":
        return "LibreOffice"
    return "no renderer"


def _timeout() -> float:
    raw = os.environ.get(_TIMEOUT_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_TIMEOUT


# ---- helpers ---------------------------------------------------------------
def _validate_pdf(path: Path) -> None:
    """A renderer exit code is not proof: the output must be a readable PDF."""
    import pymupdf
    try:
        with pymupdf.open(path) as doc:
            pages = doc.page_count
    except Exception as exc:                  # corrupt or not a PDF
        raise RenderError("the renderer produced an unreadable file") from exc
    if pages <= 0:
        raise RenderError("the renderer produced a PDF with no pages")


def _word_pids() -> set[int]:
    """PIDs of running WINWORD.EXE processes (Windows only; empty elsewhere)."""
    if sys.platform != "win32":
        return set()
    try:
        result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WINWORD.EXE",
                                 "/FO", "CSV", "/NH"],
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return set()
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        parts = [part.strip().strip('"') for part in line.split('","')]
        if len(parts) >= 2 and parts[0].strip('"').lower() == "winword.exe":
            try:
                pids.add(int(parts[1].strip('"')))
            except ValueError:
                continue
    return pids


def _kill_new_word_processes(before: set[int]) -> None:
    """Kill only WINWORD processes this render started; never a user's Word."""
    if sys.platform != "win32":
        return
    for pid in _word_pids() - before:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, timeout=15)


def _venv_safe_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)               # office macros must not see our path
    return env


def _run_process(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run a renderer subprocess; on timeout kill its process tree (Windows)."""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=_venv_safe_env(), text=True, errors="replace",
                               creationflags=creationflags)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        else:
            process.kill()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:      # pragma: no cover - kernel-stuck child
            pass
        raise RenderTimeout("the renderer did not finish in time") from None
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


# ---- Word ------------------------------------------------------------------
def _render_word(src: Path, out: Path) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        raise RenderError("Word is installed but PowerShell was not found")
    workdir = Path(tempfile.mkdtemp(prefix="legal-redact-word-"))
    before = _word_pids()
    completed = False
    try:
        script = workdir / "render.ps1"
        script.write_text(_WORD_SCRIPT, encoding="utf-8")
        env = _venv_safe_env()
        env["LEGAL_REDACT_SRC"] = str(src)
        env["LEGAL_REDACT_OUT"] = str(out)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
            errors="replace", creationflags=creationflags)
        try:
            _stdout, stderr = process.communicate(timeout=_timeout())
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=30)
            else:
                process.kill()
            raise RenderTimeout("Word did not finish rendering in time") from None
        if process.returncode != 0:
            raise RenderError("Word automation failed")
        if not out.is_file() or out.stat().st_size == 0:
            raise RenderError("Word produced no output file")
        completed = True
    finally:
        # Only on an abnormal exit: a clean run's Word was closed by the script itself,
        # and a Word the user opened by hand during the render must never be killed.
        if not completed:
            _kill_new_word_processes(before)
        shutil.rmtree(workdir, ignore_errors=True)


# ---- LibreOffice -----------------------------------------------------------
def _render_libreoffice(src: Path, out: Path) -> None:
    exe = _libreoffice_path()
    if not exe:
        raise RendererUnavailable("LibreOffice was not found")
    profile = Path(tempfile.mkdtemp(prefix="legal-redact-lo-profile-"))
    convert_dir = Path(tempfile.mkdtemp(prefix="legal-redact-lo-out-"))
    try:
        cmd = [exe, "--headless", "--norestore", "--invisible", "--nodefault",
               "--nolockcheck", "--nofirststartwizard",
               f"-env:UserInstallation={profile.as_uri()}",
               "--convert-to", "pdf:writer_pdf_Export",
               "--outdir", str(convert_dir), str(src)]
        result = _run_process(cmd, _timeout())
        produced = convert_dir / (src.stem + ".pdf")
        if not produced.is_file():
            candidates = sorted(convert_dir.glob("*.pdf"))
            if len(candidates) == 1:
                produced = candidates[0]
        if result.returncode != 0 or not produced.is_file() or produced.stat().st_size == 0:
            raise RenderError("LibreOffice could not convert the document")
        try:
            shutil.copyfile(produced, out)
        except OSError as exc:
            raise RenderError("the rendered PDF could not be copied") from exc
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(convert_dir, ignore_errors=True)


# ---- public entry point ----------------------------------------------------
def render_docx_to_pdf(src: str | Path, out: str | Path,
                       renderer: str | None = None) -> Path:
    """Render `src` to a PDF at `out`; return the path.

    `renderer` overrides detection for tests/callers that already chose. Raises
    RendererUnavailable when nothing is installed, RenderTimeout on timeout, and
    RenderError for any other failure. Never overwrites `src`.
    """
    src_path = Path(src).resolve()
    out_path = Path(out).resolve()
    if src_path == out_path:
        raise RenderError("the rendered output must be a different file from the source")
    if not src_path.is_file():
        raise RenderError("the source file does not exist")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    kind = renderer if renderer is not None else detect_renderer()
    if kind is None:
        raise RendererUnavailable("no Word or LibreOffice renderer was found")
    if kind == "word":
        _render_word(src_path, out_path)
    elif kind == "libreoffice":
        _render_libreoffice(src_path, out_path)
    else:
        raise RenderError(f"unknown renderer: {kind}")
    try:
        _validate_pdf(out_path)
    except RenderError:
        out_path.unlink(missing_ok=True)
        raise
    return out_path
