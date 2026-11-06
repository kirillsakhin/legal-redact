# DOCX support: design note (v0.3)

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
| PF113 | REVIEW | small/medium image(s) present (below the 25% page rule): text inside them is not read, images kept unchanged |
| PF114 | REVIEW | custom XML parts present; removed on export (content controls bound to them may lose values) |
| PF115 | INFO | text boxes were read and are included in redaction |
| PF116 | INFO | document metadata fields present; scrubbed on export |
| PF117 | INFO | property/thumbnail/custom-data parts and document variables will be removed |
| PF118 | INFO | what was read (parts, containers, characters) |
| PF119 | REFUSE | `altChunk` / subdocument (content merged at open time from a part we do not read) |
| PF121 | REFUSE | text-carrying XML constructs (comments, processing instructions, DOCTYPE/entity, CDATA outside text parts, non-structural values in content types/relationship attributes) |
| PF122 | REFUSE | legacy form fields (`w:ffData`) — they carry values this tool does not read |
| PF123 | REFUSE | an image covering **at least 25% of a page** (mirrors the PDF rule): it may be a pasted scan or a photographed page, and a `.docx` export cannot remove text that lives in pixels |
| PF124 | REFUSE | a package part name or a copied presentation name (style name, font family name, list-numbering format text) contains a structured personal identifier (e-mail/ID/IBAN/card pattern) |
| PF126 | INFO | presentation-only text is copied verbatim and not inspected (counts of style names, list-numbering format values, font family names, embedded fonts) |

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
| V106 | paragraph and run counts unchanged in every text part; image/font content byte-identical and present with neutral generated names; every part the writer may not edit byte-identical; nothing added or dropped that should have been kept |
| V107 | content types and relationship targets resolve — the file Word will open is not one Word has to repair |
| V108 | every output part name is structural or generated, never a source-chosen name |

A PASS means "no leak found by these checks", not "the document is safe". The report lists the DOCX
limits: text inside images, presentation-only text in copied parts, numeric layout values, duplicate
alternate-content copies, decomposed Unicode, indirect identification, and signatures.

## Review gate and CLI

`inspect`, `suggest` and `redact` take `.docx` files; the format is detected from the extension,
then from magic bytes. `review` — the browser page — renders PDF pages only; for `.docx` it prints a
clear message and exits non-zero. **DOCX review is batch-style**: the decision file carries no
document text by design, so the lawyer cannot see what they are approving in the tool; they must
open the source in Word or convert it to PDF. The decision file is a record of decisions, not a
review UI, and the `review` stage is weaker for DOCX than for PDF. Every suggestion must be set to
`accepted` or `rejected`, REVIEW findings acknowledged with `--ack`, then `redact` writes and
verifies the output. `--accept-proposed` works as for PDF. `--accept-repeats` and `--no-ocr` are
PDF-only and are ignored with a note/notice for `.docx`.

## Known gaps (deliberate, documented)

- **Images are not read.** Text, handwriting, signatures or screenshots inside an image are not
  detected. Images covering at least 25% of a page are REFUSED (a pasted scan must be redacted as a
  PDF/OCR page); smaller images are a REVIEW the lawyer acknowledges, and are copied unchanged. Image
  file metadata (EXIF/XMP inside the image bytes) is also copied unchanged.
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

## Dependencies

`lxml` is a core dependency (writer/editor parser only). `python-docx` is in the `dev` extra for
tests only: one test opens the exported file with it as an independent third-party reader. The
package never imports it.

## Tests

`tests/test_docx.py` (48 tests) covers: plain extraction and paragraph boundaries; run joining; a
name split across runs redacted in the XML; header/footer/footnote/text-box redaction; floating
(`wp:anchor`, numeric `wp:posOffset`) text boxes; glossary text; alternate content; comments
stripped after acknowledgement; metadata/custom properties/people/document variables scrubbed;
hidden carriers removed; image flagging, large-image refusal, small-image acknowledgement and the
image bytes/neutral name; embedded font renaming; each REFUSE code; hidden numeric text refused
except known layout values; PII in part names and font-face names refused; the gate blocking
undecided suggestions and unacknowledged REVIEW findings; mutation tests that disable run
replacement, skip a header copy, keep a comment part, skip the metadata scrub or keep source part
names and show the verifier withholds the file; CLI inspect/suggest/redact end-to-end with no
document text in the output; and a python-docx-generated document accepted and redacted. The full
suite (199 tests) stays green with the 151 existing PDF/OCR tests unchanged.

**Real Word smoke test (2026-11-06).** A short synthetic contract with a name split across three
runs, an e-mail, a phone number and an IBAN was redacted with the CLI, then opened read-only in
Microsoft Word 16 (COM automation, invisible) and exported to PDF. Word opened it without a repair
prompt (1 page, 23 words); the rendered PDF contained 89 U+2588 block characters and no recoverable
planted value (`Alice`, the e-mail, the phone and the IBAN were all absent from the PDF's text
layer). This checks that the edited package is a document Word accepts, not just one our own readers
accept. It is not a substitute for the adversarial fixture audit (`_legalredact_publish\docx_audit\`
in the development workspace): that audit covered 45 hostile inputs with 0 critical leaks and 0 false
refusals on this frozen tree.
