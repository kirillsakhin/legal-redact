"""The `command` engine: run a user-supplied executable once per page.

This is a compatibility/choice adapter, not an accuracy claim: the wrapper the
user configures does the reading (it may be a local model or a paid cloud
service), and the package only validates its answer and turns it into the same
page model the rest of the pipeline uses.

How a wrapper is invoked (no shell, ever -- argv is built as a list):

    <argv from the spec>  <png path>  <page width pt>  <page height pt>  <dpi>

The same three numbers are also exported as environment variables
(`LEGAL_REDACT_OCR_PAGE_WIDTH_PT`, `LEGAL_REDACT_OCR_PAGE_HEIGHT_PT`,
`LEGAL_REDACT_OCR_DPI`) for wrappers that prefer env. The wrapper writes ONE
JSON object to stdout (contract and examples: docs/OCR_BACKENDS.md) and exits 0.
Anything else -- nonzero exit, timeout, invalid JSON, a word outside the page,
a missing `local` flag -- raises OcrError: a failed read is never treated as
"no text" and never silently becomes a pass.

Locality: the engine is configured as local (`--ocr-local` / environment) or
non-local. A non-local engine needs `--allow-cloud-ocr` before any page is
rendered or sent, and each file's cloud-review finding must be acknowledged.
The JSON `local` flag must agree with the configuration; a contradiction is an
error, because it would mean the tool's own record of where pages went is wrong.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..ocr import OcrError
from .base import (ENGINE_TIMEOUT_S, EngineConfigError, MAX_STDOUT_BYTES,
                   validate_result_payload)


def split_command(command: str) -> list[str]:
    """Split a command line into argv without a shell.

    Double quotes group one argument on every platform (Windows paths with
    spaces are the common case); no expansion of variables, globs or pipes ever
    happens. Single quotes are literal on Windows and grouping on POSIX, matching
    the platform's own convention.
    """
    command = (command or "").strip()
    if not command:
        raise EngineConfigError("empty OCR engine command (use 'command:<cmdline>')")
    if os.name == "nt":
        try:
            tokens = shlex.split(command, posix=False)
        except ValueError as exc:
            raise EngineConfigError(f"cannot parse OCR engine command: {exc}") from exc
        argv = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] == '"' else t for t in tokens]
    else:
        try:
            argv = shlex.split(command, posix=True)
        except ValueError as exc:
            raise EngineConfigError(f"cannot parse OCR engine command: {exc}") from exc
    argv = [t for t in argv if t != ""]
    if not argv:
        raise EngineConfigError("empty OCR engine command")
    return argv


def _resolvable(token: str) -> bool:
    try:
        if Path(token).is_file():
            return True
    except OSError:
        pass
    return shutil.which(token) is not None


class CommandEngine:
    """Run a configured external OCR program and validate what it answers."""

    name = "command"

    def __init__(self, argv: list[str], *, local: bool, timeout: float = ENGINE_TIMEOUT_S):
        if not argv:
            raise EngineConfigError("empty OCR engine command")
        self.argv = list(argv)
        self._local = bool(local)
        self._timeout = float(timeout)
        self._last_version: str | None = None

    def available(self) -> bool:
        return bool(self.argv) and _resolvable(self.argv[0])

    def version(self) -> str | None:
        return self._last_version

    def local(self) -> bool:
        return self._local

    def analyze(self, image_png: bytes, page_width_pt: float, page_height_pt: float,
                dpi: int) -> OcrResult:
        if not isinstance(image_png, (bytes, bytearray)) or not image_png:
            raise OcrError("OCR engine received an empty page image")
        with tempfile.TemporaryDirectory(prefix="legal-redact-cmd-ocr-") as tmp:
            png = Path(tmp) / "page.png"
            png.write_bytes(bytes(image_png))
            out_path = Path(tmp) / "out.json"
            env = dict(os.environ)
            env.update({
                "LEGAL_REDACT_OCR_PAGE_WIDTH_PT": f"{float(page_width_pt):.4f}",
                "LEGAL_REDACT_OCR_PAGE_HEIGHT_PT": f"{float(page_height_pt):.4f}",
                "LEGAL_REDACT_OCR_DPI": str(int(dpi)),
            })
            cmd = [*self.argv, str(png), f"{float(page_width_pt):.4f}",
                   f"{float(page_height_pt):.4f}", str(int(dpi))]
            try:
                with open(out_path, "wb") as stdout_file:
                    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=stdout_file,
                                          stderr=subprocess.DEVNULL, env=env,
                                          timeout=self._timeout)
            except subprocess.TimeoutExpired as exc:
                raise OcrError(
                    f"OCR engine command timed out after {self._timeout:g}s; "
                    "run the wrapper manually to diagnose") from exc
            except (OSError, subprocess.SubprocessError) as exc:
                raise OcrError(
                    f"could not run the OCR engine command ({type(exc).__name__}); "
                    "run the wrapper manually to diagnose") from exc
            if proc.returncode != 0:
                raise OcrError(f"OCR engine command failed (exit {proc.returncode}); "
                               "run the wrapper manually to diagnose")
            size = out_path.stat().st_size
            if size > MAX_STDOUT_BYTES:
                raise OcrError(f"OCR engine output is too large ({size} bytes; "
                               f"limit {MAX_STDOUT_BYTES})")
            raw_bytes = out_path.read_bytes()
        try:
            raw = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OcrError("OCR engine output is not valid UTF-8") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OcrError("OCR engine output is not valid JSON") from exc
        result = validate_result_payload(payload)
        if result.local != self._local:
            raise OcrError(
                "OCR engine output contradicts its configuration: it reported "
                f"local={str(result.local).lower()} but was configured as "
                f"local={str(self._local).lower()}; refusing (this controls whether pages "
                "leave the machine)")
        self._last_version = result.version
        return result
