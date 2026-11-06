"""Fake OCR engine used only by the tests (a command-engine wrapper).

It is deliberately honest enough to stand in for a real reader: it returns a
word only when the region the word claims is not black. That makes it usable for
end-to-end redaction tests (after the writer burns a word's box, the word is no
longer "read"), without any OCR library or network call.

Invocation (the adapters append the last four arguments):

    python fake_ocr_engine.py --words words.json --mode valid [--local true|false]
        [--marker path] <page.png> <width_pt> <height_pt> <dpi>

`--words` is a JSON list of {"text", "box": [x0,y0,x1,y1] in 0..1, "confidence"}.
Modes exist for every refusal path the real adapter must handle.
"""
import argparse
import json
import sys
import time
from pathlib import Path


def _load_pix(png_path: str):
    """One decoded page image, reused for every word (a per-word decode is slow)."""
    import pymupdf

    doc = pymupdf.open(png_path)
    try:
        return doc[0].get_pixmap()
    finally:
        doc.close()


def _bright_fraction(pix, box) -> float:
    x0, y0, x1, y1 = box
    ix0, iy0 = max(0, int(x0 * pix.width)), max(0, int(y0 * pix.height))
    ix1 = min(pix.width, max(ix0 + 1, int(x1 * pix.width)))
    iy1 = min(pix.height, max(iy0 + 1, int(y1 * pix.height)))
    n = pix.n
    samples = pix.samples
    bright = total = 0
    step_x = max(1, (ix1 - ix0) // 20)
    step_y = max(1, (iy1 - iy0) // 10)
    for y in range(iy0, iy1, step_y):
        for x in range(ix0, ix1, step_x):
            base = (y * pix.width + x) * n
            value = samples[base] if n < 3 else sum(samples[base:base + 3]) / 3
            bright += value > 100
            total += 1
    return bright / total if total else 0.0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--words", default=None)
    parser.add_argument("--mode", default="valid")
    parser.add_argument("--local", default="true")
    parser.add_argument("--marker", default=None)
    parser.add_argument("png")
    parser.add_argument("width")
    parser.add_argument("height")
    parser.add_argument("dpi")
    args = parser.parse_args(argv)

    if args.marker:
        Path(args.marker).write_text("the OCR engine was invoked", encoding="utf-8")
    local = args.local.strip().lower() == "true"

    if args.mode == "crash":
        print("fake engine crashed", file=sys.stderr)
        return 7
    if args.mode == "timeout":
        time.sleep(30)
        return 0
    if args.mode == "invalid-json":
        sys.stdout.write("this is not JSON")
        return 0
    if args.mode == "wrong-local":
        local = not local                      # contradicts the configured locality
    if args.mode == "bad-rotation":
        payload = {"local": local, "engine": "fake", "version": "1.0", "rotation": 45,
                   "words": []}
        sys.stdout.write(json.dumps(payload))
        return 0

    words = []
    if args.words:
        spec = json.loads(Path(args.words).read_text(encoding="utf-8"))
        pix = _load_pix(args.png)
        for item in spec:
            present = _bright_fraction(pix, item["box"]) > 0.02
            if not present and args.mode != "force":
                continue
            words.append({"text": item["text"], "box": item["box"],
                          "confidence": item.get("confidence", 0.99)})
    if args.mode == "out-of-bounds" and words:
        words[0]["box"] = [words[0]["box"][0], words[0]["box"][1], 1.5, words[0]["box"][3]]
    if args.mode == "bad-confidence" and words:
        words[0]["confidence"] = 1.5
    if args.mode == "empty-word":
        words.insert(0, {"text": "   ", "box": [0.1, 0.1, 0.2, 0.2], "confidence": 0.9})
    if args.mode == "no-local":
        payload = {"engine": "fake", "version": "1.0", "words": words}
    else:
        payload = {"engine": "fake", "version": "1.0", "local": local,
                   "rotation": 0, "words": words,
                   "meta": {"note": "synthetic"}}
    sys.stdout.write(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
