"""Engine registry and configuration resolution.

A run selects exactly one engine specification:

- `tesseract` (the default, local, evaluated);
- `command:<cmdline>` (any external wrapper: local model or paid service).

The spec comes from, in order: an explicit argument / command-line flag, the
`LEGAL_REDACT_OCR_ENGINE` environment variable, then the default. A command
engine is treated as NON-LOCAL (third-party/cloud) unless it is declared local
with `--ocr-local` or `LEGAL_REDACT_OCR_LOCAL=1`: the safe default is to assume
page images could leave the machine. Non-local engines additionally require
`--allow-cloud-ocr` / `LEGAL_REDACT_ALLOW_CLOUD_OCR=1` before any page is
rendered or sent, and every document then needs the per-file cloud finding
acknowledged before export (see docs/OCR_BACKENDS.md).

`EngineConfig.check_gate()` performs only that approval check: it never runs the
engine, renders a page or touches the document, so a refused run does no OCR at
all.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping

from .base import (COMMAND_PREFIX, DEFAULT_ENGINE, ENV_ALLOW_CLOUD, ENV_ENGINE, ENV_LOCAL,
                   CloudOcrNotApproved, EngineConfigError, OcrEngine)
from .command import CommandEngine, split_command
from .tesseract import TesseractEngine


def _env_flag(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def canonical_spec(raw: str | None) -> str:
    """Normalise an engine specification; raises EngineConfigError when invalid."""
    spec = str(raw or "").strip()
    if not spec:
        return DEFAULT_ENGINE
    if spec.lower() == DEFAULT_ENGINE:
        return DEFAULT_ENGINE
    if spec.lower() == "command":
        raise EngineConfigError(
            "engine 'command' needs a command line: use --ocr-command \"...\" or "
            "--ocr-engine \"command:<cmdline>\" (see docs/OCR_BACKENDS.md)")
    if spec.lower().startswith(COMMAND_PREFIX):
        command = spec[len(COMMAND_PREFIX):].strip()
        if not command:
            raise EngineConfigError(
                "empty command after 'command:'; see docs/OCR_BACKENDS.md")
        return COMMAND_PREFIX + command
    raise EngineConfigError(
        f"unknown OCR engine {spec!r}: expected 'tesseract' or 'command:<cmdline>' "
        "(see docs/OCR_BACKENDS.md)")


def command_argv_from_spec(spec: str) -> list[str]:
    if not spec.startswith(COMMAND_PREFIX):
        raise EngineConfigError(f"not a command engine specification: {spec!r}")
    return split_command(spec[len(COMMAND_PREFIX):])


@lru_cache(maxsize=32)
def _cached_engine(spec: str, local: bool) -> OcrEngine:
    """One engine instance per (spec, declared locality) in this process.

    Caching matters for command engines: the version a wrapper reports is only
    known after it has run once, and suggest/export should be able to report it.
    """
    if spec == DEFAULT_ENGINE:
        return TesseractEngine()
    if spec.startswith(COMMAND_PREFIX):
        return CommandEngine(command_argv_from_spec(spec), local=local)
    raise EngineConfigError(f"unknown OCR engine specification: {spec!r}")


@dataclass(frozen=True)
class EngineConfig:
    """A resolved engine choice for one invocation."""
    spec: str
    local: bool = True
    allow_cloud: bool = False

    def engine(self) -> OcrEngine:
        return _cached_engine(self.spec, self.local)

    def check_gate(self) -> None:
        """Refuse a non-local engine that the caller has not approved.

        Pure configuration check: no engine call, no page render, no document
        read. The error names the docs and the flag.
        """
        if not self.local and not self.allow_cloud:
            raise CloudOcrNotApproved(
                "cloud/third-party OCR is not approved for this run. Passing page images to "
                "an external OCR service can compromise the document. Re-run with "
                "--allow-cloud-ocr (or LEGAL_REDACT_ALLOW_CLOUD_OCR=1) if your firm has "
                "approved the service and the transfer is lawful; see docs/OCR_BACKENDS.md. "
                "No page was rendered or sent.")

    def info(self) -> dict:
        """Engine name/version/local for inspect, the review page and the report."""
        engine = self.engine()
        return {"engine": self.spec, "name": getattr(engine, "name", "command"),
                "version": engine.version(), "local": self.local,
                "cloud_approved": self.allow_cloud}

    def pinned(self) -> dict:
        """The engine state recorded in a decision file and checked on export."""
        return {"engine": self.spec, "version": self.engine().version(),
                "local": self.local}


def resolve_config(engine: str | None = None, command: str | None = None, *,
                   allow_cloud: bool = False, local: bool | None = None,
                   env: Mapping[str, str] | None = None) -> EngineConfig:
    """Resolve the engine config for one invocation (flags > env > default).

    Does NOT enforce the cloud gate: callers decide when to print the disclaimer
    and then call `config.check_gate()` before any OCR work.
    """
    env = os.environ if env is None else env
    raw = (engine or "").strip()
    if command is not None and str(command).strip():
        if raw and raw.lower() not in ("command", COMMAND_PREFIX.rstrip(":")):
            raise EngineConfigError(
                "both --ocr-engine and --ocr-command were given; use one (or "
                "--ocr-engine \"command:<cmdline>\")")
        raw = COMMAND_PREFIX + str(command).strip()
    if not raw:
        raw = str(env.get(ENV_ENGINE, "")).strip() or DEFAULT_ENGINE
    spec = canonical_spec(raw)
    if spec == DEFAULT_ENGINE:
        return EngineConfig(DEFAULT_ENGINE, local=True, allow_cloud=False)
    declared_local = local if local is not None else _env_flag(env.get(ENV_LOCAL))
    allow = bool(allow_cloud) or _env_flag(env.get(ENV_ALLOW_CLOUD))
    return EngineConfig(spec, local=bool(declared_local), allow_cloud=allow)


DEFAULT_CONFIG = EngineConfig(DEFAULT_ENGINE, local=True, allow_cloud=False)


def default_config() -> EngineConfig:
    return DEFAULT_CONFIG
