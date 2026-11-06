"""Pluggable OCR engines (see `docs/OCR_BACKENDS.md`).

The public surface is deliberately small:

- `TesseractEngine` -- the default, local, evaluated engine;
- `CommandEngine` -- a validated adapter for a user-supplied wrapper program;
- `OcrEngine`/`OcrResult`/`OcrWord` -- the contract those implement;
- `EngineConfig`/`resolve_config` -- pick and gate one engine per invocation;
- `page_text_with_engine` -- run an engine on a page and get the pipeline's
  PageText model;
- the cloud disclaimer and approval label used verbatim in the UI, findings,
  CLI warnings and docs.

No engine is imported or run until the configuration has passed its gate:
selecting a non-local engine without `--allow-cloud-ocr` fails before any page
is rendered or sent.
"""
from .base import (CLOUD_OCR_ACK_LABEL, CLOUD_OCR_DISCLAIMER, DEFAULT_ENGINE, ENV_ALLOW_CLOUD,
                   ENV_ENGINE, ENV_LOCAL, MAX_WORD_CHARS, MAX_WORDS, CloudOcrNotApproved,
                   EngineConfigError, OcrEngine, OcrResult, OcrWord,
                   validate_result_payload)
from .command import CommandEngine, split_command
from .registry import (EngineConfig, canonical_spec, command_argv_from_spec, default_config,
                       resolve_config)
from .run import page_text_from_result, page_text_with_engine
from .tesseract import TesseractEngine

__all__ = [
    "CLOUD_OCR_ACK_LABEL", "CLOUD_OCR_DISCLAIMER", "DEFAULT_ENGINE", "ENV_ALLOW_CLOUD",
    "ENV_ENGINE", "ENV_LOCAL", "MAX_WORD_CHARS", "MAX_WORDS", "CloudOcrNotApproved",
    "CommandEngine", "EngineConfig", "EngineConfigError", "OcrEngine", "OcrResult", "OcrWord",
    "TesseractEngine", "canonical_spec", "command_argv_from_spec", "default_config",
    "page_text_from_result", "page_text_with_engine", "resolve_config", "split_command",
    "validate_result_payload",
]
