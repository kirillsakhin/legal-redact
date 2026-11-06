# DOCX support: design note (v0.5)

Word `.docx` (OOXML WordprocessingML) is the second input format. This note records what the tool
reads, what it refuses, how the export is written and checked, what is known not to be covered, and
how it was tested. It is the `.docx` companion to the threat model rows T19-T30 in
[docs/THREAT_MODEL.md](THREAT_MODEL.md).

## Why a separate pipeline

A PDF redaction is a page operation: find the words, draw a box, delete what is under it. A `.docx`
is a ZIP of XML parts with no pages at all; the text a reader sees lives in runs (`w:t`) that Word
may split anywhere, and the file also carries comments, revision history, property bags, alternate
text, link targets and settings that a reader never sees in the body. Painting over a run would
leave the characters in the XML.

So the DOCX path keeps the same five stages but replaces the format-specific ones (`extract.py`,
`preflight.py`, `redact.py`, `verify.py`) with a `docx/` package. The shared parts are untouched:
the detectors, the policy proposals, the `ReviewSession` gate and the decision-file format.

## Extraction (`docx/package.py`, `docx/extract.py`)

1. **File type by magic, not extension.** PDF, ZIP (OOXML), and CFB/OLE containers are told apart by
   the first bytes; a CFB file is scanned for the `EncryptionInfo`/`WordDocument` stream names so an
   encrypted `.docx` is refused as encrypted and a renamed `.doc` is refused as a legacy file.
2. **Package validation.** The ZIP must open, have no duplicate or path-traversal part names, no
   encrypted entries, no CRC failures, a `[Content_Types].xml`, and stay under the size caps
   (200 MB total / 64 MB per part / 4096 parts). Part names must match the OOXML URI vocabulary
   (a name is recoverable metadata, so a name carrying a space or non-URI character is refused).
   The main part comes from the content-types override and must be a plain WordprocessingML
   document.
3. **Every part is classified into a closed vocabulary.** Recognised text parts are
   `word/document.xml`, `header*.xml`, `footer*.xml`, `footnotes.xml`, `endnotes.xml` and
   `word/glossary/document.xml` (AutoText — read and redacted too, not silently copied). Inert parts
   are a fixed set: styles, numbering, settings, webSettings, fontTable, `word/theme/theme*.xml`,
   images, embedded fonts. Comments, `people.xml`, custom XML, custom properties and thumbnails are
   removed. Macros, ActiveX and embedded OLE objects are refusals. **Anything else is a refusal**
   (charts, SmartArt, `afchunk.html`, odd names under `word/theme/`, an image/font extension outside
   the allowlist, ...): an unrecognised part can carry text and this tool will not promise to scrub
   it.
4. **Text model.** For each text part, paragraphs are read in document order. Each character is
   registered against the `w:t` element that holds it (`TextRun(start, end, element)`); `w:tab` /
   `w:br` become single virtual characters so the detectors see sane spacing and line context; runs
   are concatenated with no separator (a name split across runs reads as one name); paragraphs are
   joined with `\n`. A text box (`w:txbxContent`) becomes its own container; nested boxes and both
   copies of an `mc:AlternateContent` choice/fallback are read. Floating (`wp:anchor`) drawings are
   first-class: their numeric `wp:posOffset` positioning values are geometry, not document text.
5. **Container order is deterministic.** Document body, then headers by number, footers, footnotes,
   endnotes, glossary; inside a part, the body paragraphs and then each text box. The same code runs
   on suggest and on export, so the `page`/`start`/`end` in the decision file keeps its meaning.
6. **Coverage invariant (fail closed).** Every `w:t` in a text part must belong to an indexed run,
   and every non-whitespace text node must be inside a `w:t`, inside a field-instruction element the
   writer removes, or be a numeric value of one **explicitly allowlisted drawing-layout element** —
   and nothing else. The allowlist is by tag, not namespace, and also checks the parent element:
   `wp:posOffset` under `wp:positionH`/`positionV`, and `wp14:pctWidth`/`pctHeight` under
   `wp14:sizeRelH`/`sizeRelV`, each within a plausible layout range. Any other element with text —
   including an unknown `wp:*` element, an arbitrary numeric wrapper, math (`m:t`) or DrawingML
   (`a:t`) text, form-field data — is a refusal, not a silent skip.

The detectors then run on synthetic `PageText(page=i, text=..., words=[])` objects — exactly the
same detector code as PDF, unchanged.

## Refusals and disclosures (`docx/preflight.py`)

| Code | Severity | Condition |
|---|---|---|
| PF101 | REFUSE | encrypted container (CFB `EncryptionInfo`, encrypted ZIP entries) |
| PF102 | REFUSE | legacy binary `.doc` |
| PF103 | REFUSE | `.docm`/macro-enabled main part, `vbaProject`/`vbaData`/ActiveX parts |
| PF104 | REFUSE | not a supported `.docx` package: bad ZIP, no/invalid content types, template, malformed part, unsupported extension |
| PF105 | REFUSE | tracked changes or revision elements (`w:ins`, `w:del`, move ranges, `*PrChange`, ...) |
| PF106 | REFUSE | embedded OLE/package objects, `w:object`, `w:control` |
| PF107 | REFUSE | any part outside the recognised set, or an image/font extension outside the allowlist |
| PF108 | REFUSE | text outside the supported containers (unknown element, math/DrawingML text, numeric text outside the allowlisted layout elements) |
| PF109 | REFUSE | package too large / too many parts |
| PF110 | REFUSE | duplicate part names (case-insensitive) |
| PF111 | REVIEW | hidden-text carriers present and removed on export: alternate text, tooltips, table captions, field codes, hyperlink targets, content-control metadata, inline custom XML |
| PF112 | REVIEW | comments present; removed entirely on export |
| PF113 | REVIEW | image(s) will be read with OCR (or, with `--no-ocr`/no engine: text inside them is not read and images are kept unchanged) |
| PF114 | REVIEW | custom XML parts present; removed on export (content controls bound to them may lose values) |
| PF115 | INFO | text boxes were read and are included in redaction |
| PF116 | INFO | document metadata fields present; scrubbed on export |
| PF117 | INFO | property/thumbnail/custom-data parts and document variables will be removed |
| PF118 | INFO | what was read (parts, containers, characters) |
| PF119 | REFUSE | `altChunk` / subdocument (content merged at open time from a part we do not read) |
| PF121 | REFUSE | text-carrying XML constructs (comments, processing instructions, DOCTYPE/entity, CDATA outside text parts, non-structural values in content types/relationship attributes) |
| PF122 | REFUSE | legacy form fields (`w:ffData`) — they carry values this tool does not read |
| PF123 | REFUSE | an image covering **at least 25% of a page** when no OCR engine is available/used (mirrors the PDF rule): a `.docx` export cannot remove text that lives in pixels |
| PF124 | REFUSE | a package part name or a copied presentation name (style name, font family name, list-numbering format text) contains a structured personal identifier (e-mail/ID/IBAN/card pattern) |
| PF126 | INFO | presentation-only text is copied verbatim and not inspected (counts of style names, list-numbering format values, font family names, embedded fonts) |
| PF127 | REFUSE | an embedded image cannot be read safely: unsupported format, undecodable bytes, too large to decode, or a multi-frame container (animated GIF, APNG, animated WebP, multi-page TIFF) detected from the raw bytes before any decode |
| PF128 | REFUSE | the OCR engine failed while reading an embedded image, or its word boxes do not overlap actual ink in the source pixels (untrustworthy engine output); fail closed |
| PF129 | INFO | image OCR result counts (images read, words found), rotation reports; never the OCR text |
| PF130 | REVIEW | no independent OCR reader for the output image check (Tesseract absent), so the same engine re-reads its own output; weaker check, acknowledged |
| PF131 | REVIEW | no text could be read in a small image; its pixels were **NOT** checked by the export verifier and personal data may survive — the reviewer must open the image and acknowledge explicitly |
| PF132 | REFUSE | the OCR engine found no text in a large/content-bearing image, or its displayed size cannot be established; such an image cannot be certified |
| PF133 | REVIEW | an image is displayed at an effective resolution below ~150 dpi, **or** a large share of its OCR word boxes are very small at native resolution (<12 px high even if the engine returned some misread words); OCR may miss that text — check the image and acknowledge. The export report's V112 WARN names every such image |
| PF019 | REVIEW | a non-local (cloud/third-party) OCR engine is selected: embedded images leave the machine; verbatim disclaimer, per-file acknowledgement |

Findings carry counts, codes and structural labels only, never document text. An export is blocked
while any REFUSE exists or any REVIEW code has not been acknowledged (`--ack PF112` etc.), on top of
the shared rule that every suggestion must be decided.

## Writer (`docx/write.py`, lxml)

- **Character replacement.** For each accepted span, every overlapping `w:t` gets its slice replaced
  by the same number of U+2588 FULL BLOCK characters and `xml:space="preserve"`. Replacement length
  equals removed length, so offsets stay valid while several spans are applied. No run is deleted or
  added: paragraph and run counts are preserved, which the verifier checks. Structural characters
  (tab, explicit line break) inside a span are left in place — they carry no document text.
- **Hidden carriers.** Comments (`word/comments*.xml`, range marks, references, author names in
  `people.xml`), custom XML parts and inline bindings/attributes, alternate text/tooltips/table
  captions/shape names, field instructions (`w:instrText`, `w:fldSimple/@w:instr`), hyperlinks
  (unwrapped to plain text, external relationships dropped), content-control alias/tag, document
  variables, mail-merge and attached-template settings, bookmark names (renamed to
  `_redacted_bm_N`), XML comments and processing instructions.
- **Metadata.** `docProps/core.xml` and `docProps/app.xml` are rebuilt as empty property roots (every
  value, child element name and attribute is gone), `docProps/custom.xml`, `word/people.xml` and
  `docProps/thumbnail.*` are removed, content-type overrides and relationships that point at removed
  parts are removed, and relationships to missing parts are dropped (so Word does not have to repair
  the file).
- **Part names are neutralised.** Images and embedded fonts are renamed to generated neutral names
  (`word/media/redacted_N.png`, `word/fonts/redacted_font_N.odttf`), with every relationship and
  content-type override updated. A ZIP entry name is recoverable text, so a source-chosen filename
  is not carried into the output; a name that reads like a personal identifier refuses the file
  before that (PF124).
- **Everything else is copied.** Parts the writer does not edit are copied byte-for-byte; ZIP entry
  order, compression method, timestamps and attributes are preserved. The output is a new same-format
  `.docx`; the source is never overwritten.
- **Defence in depth.** The writer re-runs pre-flight itself and refuses if the input would be
  refused, and it validates/coerces decision offsets before writing. A decision pointing outside the
  document text aborts the export instead of guessing.

## Images: OCR and pixel redaction (v0.5)

`docx/images.py` treats each embedded image as a one-page document at a fixed geometry convention:
its native pixels are a page at 300 dpi (a 400x200 px image is a 288x144 pt page). The image is
decoded with PyMuPDF (no new dependency), rendered to PNG, and passed to the same OCR engine
abstraction as scanned PDFs (`OcrEngine.analyze(png, width_pt, height_pt, 300)`, see
`docs/OCR_BACKENDS.md`); the engine's normalised word boxes map back to image pixels. Images become
additional text containers after the .docx text containers, so the shared detectors, policy,
decision file and review gate work unchanged — a decision still stores only a container number and
character offsets, never pixels and never text.

- **Pre-flight.** Multi-frame containers are detected from the raw bytes **before** any decode:
  animated GIF (image-descriptor count), APNG (`acTL` chunk), animated WebP (`ANIM`/`ANMF`, VP8X
  animation flag) and multi-page TIFF are REFUSED (PF127) — only one frame would be readable, so a
  frame could leak; a malformed container whose frame count cannot be established is refused too. A
  single-frame GIF is accepted and normalised to PNG. Unsupported, corrupt and oversized images are
  refused (PF127) even with `--no-ocr`, because every image must be decodable to be re-encoded.
  With a usable engine, images are decoded and read before review: PF113 says the pixels are being
  read (and what OCR cannot understand), PF129 reports counts, PF130 requires an acknowledgement
  when no independent output reader exists. If the engine finds **no words** in an image, a large /
  content-bearing image is REFUSED (PF132: it cannot be certified) and a small one needs an explicit
  acknowledgement (PF131); an image displayed at an effective resolution below about 150 dpi, or
  whose OCR word boxes are very small at native resolution (tiny text the engine may misread even
  when it returns words), needs a PF133 acknowledgement. If the engine's word boxes do not overlap
  actual ink in the source pixels (a weak or dishonest engine), the file is REFUSED (PF128). Without
  an engine or with `--no-ocr`, no OCR rule applies: PF113 review for small images, PF123 refusal for
  page-sized ones. A non-local engine needs the usual double gate (`--allow-cloud-ocr` plus the
  per-file PF019 acknowledgement).
- **Writer.** **Every** image is decoded and re-encoded as lossless PNG with the same pixel
  dimensions: accepted word boxes are painted black first (rounded outwards, one extra pixel for
  antialiased edges), transparency is flattened onto white and every non-critical PNG chunk
  (EXIF/XMP/ICC/DPI/text metadata) is stripped. This applies to untouched images too, so their pixels
  are preserved but their bytes (and GPS/author/ICC metadata) are not. Parts are renamed to a
  canonical generated name in sorted source order (`word/media/redacted_N.png`, `N` starting at 1),
  so the verifier can rebuild the source→output pairing exactly instead of guessing by dimensions;
  the content type becomes `image/png`.
- **Verifier.** V109 rebuilds the canonical source→output pairing, re-decodes each image, checks the
  same pixel dimensions, that every accepted area is black and that every untouched image is
  pixel-identical; V110 re-reads **every** exported image with the preferred reader (Tesseract when
  present, otherwise the configured engine with the same-engine label and the PF130 acknowledgement)
  and fails if an accepted image passage is still readable anywhere; V111 fails if any image carries
  metadata or any original image bytes survive anywhere in the package; V112 is a **WARN** that lists
  every exported image whose text was never read (0 words, or OCR not used) **or whose text is too
  small to read reliably** (the same PF133 condition), with per-image wording, so a PASS is visibly
  qualified. V106 requires the image count to be preserved with generated names (pixel equality is
  V109's job).
- **Export cross-image check.** Export re-reads **all** source images (not only the ones with
  accepted decisions) and refuses before writing if an accepted image passage is still readable in
  another image — every copy of the text must be accepted or the file is withheld.
- **Review.** Image suggestions appear like text suggestions, labelled "image N (WxH px, K words)"
  and never by source part name. The rendered review page shows the page image but draws no box for
  an image span (its position on the rendered page is not resolvable from the OOXML drawing without
  guessing); the text review lists them with an in-memory OCR context excerpt. Manual boxes remain a
  rendered-page/text-container feature; a box over image pixels is rejected rather than guessed.
- **Engine pinning.** A decision file that redacts image text records the OCR engine that produced
  it; export re-reads the source images and refuses (PF022, or a clear refusal) when the engine or
  its declared locality differs, because offsets from a different reader are not trusted.

## Verifier (`docx/verify.py`) — independent and fail-closed

The verifier uses **different readers from the writer**: stdlib `zipfile`, stdlib
`xml.etree.ElementTree` and raw-byte scanning — never lxml. It never raises: an internal error
becomes a FAIL and the file is withheld. Checks:

| Check | What it proves |
|---|---|
| V101 | output is a readable OOXML package; every XML part parses; a main text part exists |
| V102 | no redacted passage remains in the text or attribute values of any part (normalised match + compact match for line-wrapped text; name-fragment matches are warnings) |
| V103 | no redacted passage remains in the raw bytes of any decompressed part (literal, lower-case and XML-unescaped forms) |
| V104 | no comments, revision/deleted text, macros, embedded objects, external relationships, alternate-text/field-code/hyperlink/content-control carriers, XML comments/PIs/DOCTYPE/CDATA, or non-structural content-type/relationship values survive |
| V105 | core/app property parts are empty (no value, child or attribute); no custom properties, thumbnail or comment-author part survives |
| V106 | paragraph and run counts unchanged in every text part; image count preserved with neutral generated names (pixels are checked by V109); every part the writer may not edit byte-identical; nothing added or dropped that should have been kept |
| V107 | content types and relationship targets resolve — the file Word will open is not one Word has to repair |
| V108 | every output part name is structural or generated, never a source-chosen name |
| V109 | the canonical source→output image pairing is rebuilt from the source package; every image re-decodes at the same pixel size; every accepted area is black and every untouched image is pixel-identical (bytes may differ because metadata is stripped) |
| V110 | every exported image re-read with an independent reader (Tesseract preferred; same-engine only with the PF130 acknowledgement) contains none of the accepted image passages |
| V111 | no image carries metadata (EXIF/XMP/ICC/DPI/text chunks) and no original image bytes survive anywhere in the package |
| V112 | **WARN only** — lists every exported image that was not fully verified: text never read (OCR found no words, or OCR was not used) **or text too small to read reliably** (the PF133 tiny-text condition). Per-image wording says "text not read" or "text was very small ... pixels were NOT verified for hidden text"; the export still passes if everything else checks out, but the PASS line is visibly qualified and the WARN row appears in the evidence report, never silent |

A PASS means "no leak found by these checks", not "the document is safe". The report lists the DOCX
limits: OCR misreads inside images (and handwriting/stamps/photographs it does not understand),
presentation-only text in copied parts, numeric layout values, duplicate alternate-content copies,
decomposed Unicode, the image-check false-alarm case, indirect identification, and signatures.

## Review page: rendered or text (v0.5)

`legal-redact review file.docx` opens the same local review page as PDF. Two modes are chosen
automatically, and the tool never draws a box it cannot justify:

**Rendered.** When Microsoft Word (Windows COM) or LibreOffice is installed, the .docx is converted
to a PDF in a temporary directory and page images of that render are shown. Detector suggestions are
drawn where the aligned text appears. The rendered PDF is a **view only**: export always runs the
DOCX writer and the V101-V108 verifier on the source; the rendered PDF is never edited.

- Word (preferred on Windows): invisible instance, read-only open, macros force-disabled, link
  updates off, `ExportAsFixedFormat`, then close and quit; if COM attached to an already-running Word
  instance it is not quit, so the user's open Word is never closed. Timeout 120 s
  (`LEGAL_REDACT_RENDER_TIMEOUT`).
- LibreOffice: `soffice --headless --convert-to pdf` with a unique user-profile directory so
  concurrent runs cannot lock each other.
- Detection only: nothing is installed or downloaded. If neither program is present, the page uses
  text mode. `LEGAL_REDACT_RENDERER=word|libreoffice|none` forces the choice (or forces text mode),
  `LEGAL_REDACT_SOFFICE` points at a particular `soffice` binary, and `LEGAL_REDACT_RENDER_TIMEOUT`
  sets the timeout in seconds.

**Text.** Without a renderer — or when any suggestion cannot be aligned to the rendered pages with
confidence — the page lists each suggestion with a short context excerpt, accept/reject controls,
bulk accept, pre-flight acknowledgement and export. This is a first-class review mode, not an error
page. The excerpts are in-memory UI data only: they are never written to the decision file, reports
or logs.

### Fail-closed alignment (`docx/align.py`)

The .docx text model (container + character offsets) is aligned to the rendered PDF at token level:
NFKC/casefold token equality, per-container monotone anchoring on tokens unique on both sides, local
sequence matching between anchors, and a context requirement (a mapped span needs an aligned
neighbouring token nearby). A suggestion is drawn only when **every** token it touches is aligned and
the mapped boxes are monotone and on valid pages. Rendered mode is used only when at least 90% of the
.docx text could be aligned and **all** suggestions map cleanly; otherwise the page switches to text
review and says why. A span that would produce a possibly-wrong box, or two suggestions that map to
the same rendered text, is never drawn.

Manual boxes exist only in rendered mode. A drawn box is accepted only if it fully contains whole
words, those words are contiguous in one .docx container, they are in document order, and the
resulting span maps forward again; otherwise the page reports a clear rejection ("the box only
partly covers a word", "the box covers text that is not part of the .docx text model — for example a
page number", "the box covers a non-contiguous phrase", ...). An accepted manual box becomes an
ordinary manual decision with a container span and character offsets, like the PDF flow's manual
boxes but stored as text offsets.

A suggestion inside an embedded image appears in the review page as an ordinary decision labelled
"image N (WxH px, K words)" (never by source part name). The rendered page shows the image itself but
draws no box for it — an OOXML drawing does not say where its pixels land on the rendered page, and a
guessed box is worse than none; the text review lists it with an in-memory OCR context excerpt. The
image text is read when the review session starts, with the same engine that suggest used.

`inspect` reports `review_page=rendered (word)`, `review_page=rendered (libreoffice)` or
`review_page=text`; the review page itself states which mode it is in and why.

### Renderer and privacy

The renderer is an external program the user already trusts with their documents: Word or
LibreOffice reads the source .docx and writes a temporary PDF. Macros are force-disabled and link
updates are turned off before Word opens the file, but a renderer may still resolve linked content
the source document references — use an offline renderer environment if that matters. The rendered
PDF contains the unredacted document in the system temp directory; it is deleted when the session
ends (or the render fails), but another process running as the same user could read it while the
review page is open, and a crash can leave it behind for OS temp cleanup. The review API returns page
images and, in text mode, short context excerpts; the decisions file stays text-free as before.

Every suggestion must still be set to `accepted` or `rejected`, REVIEW findings acknowledged with
`--ack`, then `redact` (or the page's export button) writes and verifies the output.
`--accept-proposed` works as for PDF. `--accept-repeats` is PDF-only and is ignored with a note for
`.docx`; `--no-ocr` disables reading text inside `.docx` images (the old fail-closed image rule
applies) and still refuses image-bearing documents whose text cannot be read when it is large.

## Known gaps (deliberate, documented)

- **Image reading is OCR, with OCR's limits.** Text inside an embedded image is read with local
  English OCR (or the engine you configured) and can be redacted, but a misread character is
  invisible to detection as well as to the output check; handwriting, signatures, stamps,
  photographs and text at very low resolution are not understood. An image in which the engine finds
  no words is refused when it is large/content-bearing (PF132) and needs an explicit acknowledgement
  when it is small (PF131); an image displayed well below ~150 dpi is a REVIEW (PF133). Animated
  GIF, APNG, animated WebP and multi-page TIFF are refused (PF127) because only one frame could be
  read; an undecodable/unsupported/oversized image is refused too, never silently copied. With
  `--no-ocr` or no engine no image text is read; small images are acknowledged, page-sized ones are
  refused.
- **Image re-encoding.** **Every** image in an export is re-encoded as lossless PNG at the same pixel
  dimensions (format changes from JPEG/GIF/etc. to PNG; file size may grow), transparency is
  flattened onto white and all metadata (EXIF GPS, XMP, ICC, DPI, text chunks) is dropped. An
  untouched image is therefore **pixel-identical, not byte-identical**; its pixels are preserved but
  its original bytes and metadata are gone. Redacted images are painted black in the accepted areas
  first. Image parts are renamed canonically (`word/media/redacted_N.png` in sorted source order).
- **Image placement is not mapped in the rendered review page.** Image suggestions are listed with
  their OCR context but drawn without a box: the OOXML drawing does not say where its pixels land on
  the rendered page, and guessing could point at the wrong area. Manual boxes cannot target image
  pixels; the image path is a decision-list workflow.
- **Image OCR false alarms.** The output image check uses the same compact-match rule as the PDF
  raster check; when a redacted phrase is a compact prefix of unredacted text in another image (for
  example a name next to an e-mail address containing it) the check can raise a false alarm and
  withhold the output rather than release it.
- **Unread and tiny-text images are acknowledged blind spots, not certified pixels.** PF131 (0 words
  in a small image) and PF133 (very small text, or displayed below ~150 dpi) are explicit
  acknowledgements, not proof that the image is text-free: a human must open every such image before
  acknowledging. Every export report also carries a **WARN V112** naming each image whose text was
  never read or was too small to read reliably, so a PASS never silently claims those pixels were
  checked; the same WARN row is in the evidence report. Large unread images are
  refused instead (PF132). OCR cannot be made to read handwriting, arbitrary skew or sub-resolution
  text; redact those as a PDF/OCR scan. Ink evidence is contrast/variance-based inside the engine's
  word boxes, so low-contrast glyphs are accepted while uniform/blank boxes are refused; an engine
  that invents words over genuinely textured but non-text pixels cannot be distinguished.
- **Presentation-only text in copied parts.** Style names (`word/styles.xml`), list-numbering format
  text (`word/numbering.xml`, rendered as list labels), embedded font family names
  (`word/fontTable.xml`) and most theme/settings values are copied verbatim. PF126 reports how much
  exists. Structured identifiers among them refuse the file (PF124); a person-shaped name in a style
  or font name (`Ivanova`) is not detected — it is disclosed but not redacted. Renaming style/font
  definitions would change the document's appearance, so this is a documented limit rather than a
  writer action. PF126 is **informational and does not block export**: a batch flow that accepts every
  suggestion and acknowledges every REVIEW can release a file whose presentation names contain a
  person-shaped string. Read the PF126 count before releasing a batch export.
- **Numeric layout values.** A numeric value is only treated as geometry in the explicit allowlist
  above (`wp:posOffset` under a position element, `wp14:pctWidth`/`pctHeight` under a size-relative
  element) and only within a plausible range; a plausible-looking number hidden there is copied, not
  redacted. Numeric text anywhere else — including an unknown `wp:*` element — refuses the file
  (PF108).
- **Some carriers are removed by design, not redacted.** Comments are deleted whole (they are not
  part of the body the detectors read), hyperlinks become plain text, field instructions are lost
  (cached field results remain as normal text), custom XML is dropped, bookmarks are renamed,
  document properties are emptied. Each is surfaced as a REVIEW/INFO finding before export.
- **Decomposed Unicode (NFD).** A name written in a decomposed form (base letter + combining mark)
  can evade the detectors, which see the extracted code points; no NFC/NFD-normalised detection view
  with an offset map is built. PDF has the same detection-level gap. Add it to the detection work,
  not to the DOCX extractor (which would break offset mapping).
- **Rendered review depends on the renderer.** Word can open a file in Protected View, refuse a
  document it considers damaged, or show a repair prompt that automation cannot accept; LibreOffice
  can lay out some constructs differently. Any of that only affects the page shown: the page falls
  back to text review and export still uses the fail-closed .docx writer/verifier. LibreOffice was
  implemented and unit-tested but not exercised on a machine with LibreOffice installed (this
  development machine has Word only); the Word path is exercised end-to-end. The renderer is
  detected, never installed.
- **Temporary rendered PDF.** It holds the unredacted document while the review page is open, in the
  system temp directory, and is deleted at session end or when a render fails. Same-user processes
  could read it during review; a crash can leave it for OS cleanup. Nothing is written into the
  output folder and the rendered PDF is never the exported artifact.
- **Alignment is token-based.** Layout reordering (multi-column text, floating anchors, field results
  that change at render time), words split differently by the renderer, or decomposed Unicode can
  reduce alignment coverage. In those cases the affected suggestion — or the whole document, if any
  suggestion is affected — falls back to text review rather than drawing a possibly-wrong box.
  Manual boxes cover whole words only; a box drawn through part of a word is rejected, not rounded.
- **One .docx span, one box.** Headers, footers and text boxes are single text containers in the
  .docx model even when the renderer repeats them on every page; a box for a header/footer span is
  drawn at its first rendered occurrence. Redaction still removes every character of that container.
  A spanned header/footer decision therefore looks like one box per container, not one per page.
- **Two simultaneous Word review sessions can interfere.** The renderer tracks the Word processes its
  own COM automation starts and quits/kills only those; if two review sessions render with Word at
  the same moment, one may lose its Word instance. The failed render degrades to text review and
  export is unaffected (fail-closed). LibreOffice avoids profile locking with a unique profile but
  the same caution applies to resource contention.
- **AlternateContent duplication.** Word stores a drawing's content twice (choice + fallback); both
  copies are read and redacted, which is safe but can over-redact, and a span in one copy may not
  join a span in the other.
- **Refused rather than guessed:** encrypted, `.doc`, `.docm`/`.dotx`, macros, tracked changes,
  embedded OLE, legacy form fields, `altChunk`, unknown parts (charts/SmartArt), unsupported
  extension, packages with unexpected structure, large images.
- **Not tested on real Word documents.** Tested on hand-built OOXML fixtures (adversarial shapes:
  split runs, floating text boxes, comments, tracked changes, carriers, metadata, numeric hidden
  text, part-name PII) and documents generated by python-docx; not on real client contracts.
- **Document protection and signatures** are not preserved or assessed.

## Performance

Measured on Windows 11, Python 3.14.7, a synthetic 3-page-equivalent python-docx contract (heading,
12 clauses, table, header, footer; 19 suggestions): pre-flight about 24 ms (it parses the
style/font tables to report presentation text), extraction 2 ms, in-process suggest 26 ms, export +
independent verification 136 ms (the verifier iterates every element of every part). A CLI command
in a fresh process is about 0.26-0.37 s, dominated by Python/PyMuPDF import time. Timings are
wall-clock on one machine, best of five.

Review page (committed 2-page fixture, Word COM available): the one-off Word render costs about
5.8-7.3 s per review session on this machine (Word start-up plus export; measured over several runs)
and produces the ~190 KB rendered PDF; alignment takes about 24 ms for 385 rendered tokens; the
review state (pages boxes) is under 1 ms after that; a page image at 110 dpi takes about 35 ms and is
cached. Without a renderer, the text review starts in about 30 ms. The render is paid once per review
session, not per suggestion or per page view; export is unaffected by it.

Images (real local Tesseract, one 800x600 image containing three lines): reading that image once
takes about 0.1-0.2 s including Tesseract start-up per OCR pass. A CLI run on that document took
about 0.40 s for `inspect`, 0.51 s for `suggest` (pre-flight reads the image and extraction reads it
again for detection) and 0.80 s for `redact` before the audit fixes; after them export also re-reads
every source image (cross-image check) and re-verifies every output image, so expect roughly
0.1-0.2 s more per image on export. The image path re-reads images several times by design (decision
files store no text and no pixels); with a command engine the cost is one engine invocation per pass
per image. No timings are promises: they are wall clock on one machine.

## Dependencies

`lxml` is a core dependency (writer/editor parser only). `python-docx` is in the `dev` extra for
tests only: one test opens the exported file with it as an independent third-party reader. The
package never imports it.

## Tests

`tests/test_docx.py` (48 tests) covers: plain extraction and paragraph boundaries; run joining; a
name split across runs redacted in the XML; header/footer/footnote/text-box redaction; floating
(`wp:anchor`, numeric `wp:posOffset`) text boxes; glossary text; alternate content; comments
stripped after acknowledgement; metadata/custom properties/people/document variables scrubbed;
hidden carriers removed; the pre-OCR image flagging/refusal rules (`ocr=False`); embedded font
renaming; each REFUSE code; hidden numeric text refused
except known layout values; PII in part names and font-face names refused; the gate blocking
undecided suggestions and unacknowledged REVIEW findings; mutation tests that disable run
replacement, skip a header copy, keep a comment part, skip the metadata scrub or keep source part
names and show the verifier withholds the file; CLI inspect/suggest/redact end-to-end with no
document text in the output; and a python-docx-generated document accepted and redacted. The full
suite (323 tests in this checkout; the number grows as tests are added) stays green.

`tests/test_docx_images.py` (25 tests) covers the image path without requiring Tesseract, Word or
LibreOffice on CI: the fake command engine (which honestly stops reading a region after it is
burned) drives suggesting, burning and re-verification. Tests cover scan/decode and the 300-dpi
geometry convention; undecodable/oversized/unsupported images and engine failures refused; the
`--no-ocr` fail-closed rule; pre-flight counts that never quote OCR text; suggest reaching image
containers and pinning the engine; a full export whose output image is black in every accepted area,
has the original image bytes gone, and passes V109/V110/V111; untouched-image pixel identity and
metadata-free re-encoding; refusal without OCR, with a different engine, and with an unpinned
decision file; mutation tests (no burn, leftover EXIF) withheld by the verifier; text review listing
image identity plus in-memory context and rendered review listing image suggestions without a
guessing box (committed Word fixture); cloud-engine disclaimer/acknowledgement and same-engine PF130;
CLI inspect/suggest/redact plumbed through the engine flags; and one real-Tesseract end-to-end test
that skips where Tesseract is absent.

`tests/test_docx_image_hardening.py` (24 tests) turns the v0.5 independent-audit findings F1-F5 and
the round-2 residuals into regressions, using the auditor's synthetic fixtures copied into
`tests/fixtures/docx_audit/`:
multi-frame GIF/APNG/animated-WebP byte parsing and refusal (plus a malformed container failing
closed), single-frame GIF acceptance, the four audit multi-frame `.docx` fixtures refused with no
output, zero-word large images refused (PF132), zero-word small images (PF131) and low-resolution
images (PF133) requiring acknowledgement, dishonest-engine words on uniform/blank pixels refused
(PF128), an accepted image passage still visible in another image refused at export and independently
by V110 across all output images, two same-size images exporting PASS with canonical (not
dimension-based) pairing, an image-count tamper failing V109, an untouched metadata image exported
metadata-free with pixel equality, PNG tEXt/pHYs strips, and edited+untouched images both re-encoded.
Round-2 tests cover the low-contrast `I10` fixture exporting PASS with contrast-based ink evidence
(while uniform blank boxes still refuse), the tiny-text `I22` fixture going through a PF133
acknowledgement and export **with a V112 WARN naming that image (in the report and in the evidence
file's check row)**, the strengthened PF131 "pixels were NOT checked" wording, the V112 WARN naming
unread images (including when OCR is not used at all), and a unit test that contrast-based ink
accepts low-contrast glyphs but returns zero for a uniform box.

`tests/test_docx_review.py` (26 tests) covers the review page: renderer detection and labels
(monkeypatched availability), LibreOffice command construction with a unique profile plus timeout and
failure handling, alignment against the committed pre-rendered fixture (global ratio, per-suggestion
boxes, word identity, reverse mapping round-trip, partial/empty/unmapped box rejection, isolated
matches and non-matching documents failing closed), rendered review state and the full HTTP
decide/ack/export round-trip with a mocked renderer (V101-V108 PASS, no document text in the review
API in rendered mode), manual-box reverse mapping and export, the text fallback (context excerpts in
memory only, never in the decisions file; export works; unacknowledged findings block it), blocked
pre-flight skipping the renderer, CLI dispatch/inspect output, and the two-mode HTML.

**Real Word smoke test (2026-11-06).** A short synthetic contract with a name split across three
runs, an e-mail, a phone number and an IBAN was redacted with the CLI, then opened read-only in
Microsoft Word 16 (COM automation, invisible) and exported to PDF. Word opened it without a repair
prompt (1 page, 23 words); the rendered PDF contained 89 U+2588 block characters and no recoverable
planted value (`Alice`, the e-mail, the phone and the IBAN were all absent from the PDF's text
layer). This checks that the edited package is a document Word accepts, not just one our own readers
accept. It is not a substitute for the adversarial fixture audit (`_legalredact_publish\docx_audit\`
in the development workspace): that audit covered 45 hostile inputs with 0 critical leaks and 0 false
refusals on this frozen tree.

**Rendered review smoke test (2026-11-06, v0.5).** `legal-redact review
tests/fixtures/docx_review/contract.docx --policy counterparty` was started on this machine (Word
available) and driven over its local HTTP API and in a browser:
- `inspect` reported `review_page=rendered (word)`; the page reported "Review mode: rendered page
  (word)"; `/api/state` returned `review_mode=rendered`, 2 rendered pages, 8 decisions and 8 mapped
  boxes for the rules detector (12 decisions with the shipped ensemble detector, including PERSON,
  ADDRESS, DOB); the state contained no document text.
- Browser (Chromium via Playwright): page 1 showed the Word layout with boxes exactly on the e-mail,
  phone, address, DOB and the table's e-mail/phone/account cells; page 2 showed the signature and the
  floating text box; the mode banner, `p1` labels and confidence were shown; pressing `a` accepted a
  suggestion (counts changed to 7 need a decision / 1 accepted). The rendered PDF was round-tripped
  before this (Word), and no Word process was left running.
- Export through the page with the ensemble detector wrote a 34.9 KB `.docx`; V101-V108 all PASS;
  the exported package contained none of `Alice Example`, `alice@example.test`, `+1 555 0100`,
  `GB53 PSCR 5001 9062 0132 78`, `12 May 1985`, `82 Station Lane, Northvale`, `Bob Sample`; the
  decision file was text-free. Wall clock for that run: 32.5 s including ~26 s of one-off detector
  model load and ~6 s Word render.
- Forced text fallback (`LEGAL_REDACT_RENDERER=none`): the page reported "Review mode: text review
  (no page image)", showed suggestions labelled by part ("main text · 99%") with in-memory context
  excerpts and no draw/repeats controls; `/api/add` was rejected with a clear message; export wrote
  the same verified `.docx`; the decisions file stayed text-free. Start-up under a second.
- PDF review regression: a PDF review session still showed page controls, the draw button, "Find
  repeats" and its boxes; the mode banner stayed hidden. PDF review behaviour was not changed.

**Real image OCR smoke test (2026-11-06, v0.5).** A 800x600 PNG with "Alice Example",
"alice@example.test" and "12 May 1985" was embedded in a synthetic `.docx`; Tesseract 5.5.3 was
installed, no other engine:
- `inspect` (0.40 s, fresh process) reported `images=1`, `image_ocr=available (engine=tesseract)`,
  PF113 (OCR will read the image) and PF129 ("6 word(s) found in 1 of 1 image(s)"); no OCR text in
  the output.
- `suggest --detector rules` (0.51 s) wrote one decision on page 1 (the image container), no
  document text in the decision file, engine pinned.
- `redact --ack PF113` (0.80 s) passed V101-V111: V109 "1 redacted image(s) re-decoded at the same
  pixel size; 1 redacted area(s) are black", V110 "none of 1 redacted image passage(s) readable by
  Tesseract (independent reader)", V111 "no EXIF/XMP/DPI metadata and no original image bytes". The
  report named the engine and the independent reader.
- The output image was re-encoded as 800x600 `word/media/redacted_1.png`; the original image bytes
  appeared in no part of the package, the output PNG carried no metadata chunks, Tesseract no longer
  read the redacted e-mail address from the output image (the other lines were readable, unchanged).
- Text-review state showed the suggestion as `image 1 (800x600 px, 6 word(s))` with an in-memory
  OCR context excerpt, no boxes, and never the source part name.

**v0.5.0 audit-fix evidence (2026-11-06).** After the independent audit's F1-F5 fixes, the same
targeted probes were re-run on this machine:
- F2/F5 (multi-frame): the four audit fixtures whose frames carried PII (`I06`, `I18`, `I20` APNG,
  `I23`) are REFUSED by `inspect` with `PF127` ("animated GIF/APNG ... only one frame can be read")
  in 0.22-0.24 s, and `redact` exits 1 with no output file (the audit previously saw them export
  with PASS). A single-frame GIF is accepted and normalised to PNG.
- F1 (false refusal): two 400x200 images with the same pixel dimensions, different corner colours
  and both text lines redacted export PASS; the verifier's V109 reports "2 image(s) matched by
  canonical name and re-decoded at the same pixel size; 6 redacted area(s) are black; untouched
  images are pixel-identical", and the red/blue corner markers stay with the correct output image
  (`redacted_1.png` / `redacted_2.png`).
- F3 (unread images): a large image in which the engine finds no words is REFUSE `PF132`; a small
  one needs `PF131` acknowledgement ("pixels were NOT checked ... personal data may survive"); `I21`
  (45-degree rotated text, real Tesseract finds 0 words) is `PF131`, and the export after the
  acknowledgement carries a V112 WARN naming the image; an engine that returns words on uniform
  blank pixels is REFUSE `PF128`. `I22` (tiny text, partial/misread words) is `PF133` (very small
  OCR word boxes) and needs an explicit acknowledgement before export; its export report carries a
  V112 WARN ("text was very small ... pixels were NOT verified for hidden text") and the evidence
  report has the same WARN row. An accepted image passage
  that is still readable in another image is refused by export, and V110 re-reads every exported
  image.
- F4 (metadata): audit `I03` (blank image with EXIF GPS/XMP/ICC) is a small zero-word image, so
  `PF131` is acknowledged and the export PASSes; the output image is `word/media/redacted_1.png`,
  pixel-identical to the source (600x400), contains none of `EXIFCANARY`/`XMPCANARY`/`ICCCANARY` and
  no `Exif`/`pHYs`/`iCCP` markers, and no source image bytes appear anywhere in the package.
- Timing after the fixes (real Tesseract, one 800x600 image): `inspect` 0.59 s, `suggest` 0.87 s,
  `redact --ack PF113,PF130` 1.44 s (export now re-reads every source image and re-verifies every
  output image) — all V101-V111 PASS.

**v0.5.0 round-2 evidence (unread-image disclosure + ink-evidence regression).**
- R1a (0-word small): `I21` real Tesseract -> `PF131` with the strengthened wording, `inspect` 0.7 s;
  after accepting/acknowledging, `redact` PASSes but the report contains **V112 WARN "1 image(s) were
  not fully verified: image 1 (1006x1006 px): text not read; its pixels were NOT verified for
  text"**, so the PASS is visibly qualified. With `--no-ocr` the same export names every image in
  V112 ("text not read (OCR was not used for this export)").
- R1b (tiny text): `I22` real Tesseract returns 13 misread words, 46% of them under 12 px high ->
  `PF133` REVIEW ("... very small text ... OCR may miss that text and it would then not be
  redacted"); export is blocked until the acknowledgement and then PASSes with **V112 WARN "1
  image(s) were not fully verified: image 1 (1800x500 px): text was very small, OCR may have missed
  it; its pixels were NOT verified for hidden text"**, and the evidence report written with
  `--evidence` contains the same `| V112 ... | WARN |` row.
- R2 (ink evidence): `I10` low-contrast text (glyph range 20/255, no "dark" pixel) previously
  refused by PF128; now the contrast/variance-based test sees 0.23-0.37 of sampled pixels differing
  from the box median (>8 levels), so pre-flight is clean and the export PASSes V101-V112 with the
  image text redacted. Pure uniform blank boxes still score 0 and a dishonest engine returning words
  on them is still REFUSE PF128.
