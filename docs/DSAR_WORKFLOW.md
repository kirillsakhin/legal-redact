# DSAR / disclosure redaction workflow

A step-by-step way to use `legal-redact` for a disclosure or data-subject-request-style redaction,
including the records to keep. It is a workflow, **not** legal advice and **not** a compliance
certificate: whether to disclose, what exemptions apply and what the law requires remain the
controller's and the lawyer's decisions (see [COMPLIANCE.md](COMPLIANCE.md)).

## Before you start

- Confirm, with the responsible lawyer, that the disclosure is authorised and which policy applies.
  The tool cannot decide this and does not ask.
- Know that the tool **cannot tell the requester's own data from other people's data**. It redacts
  every personal value it finds; you must keep the requester's own data by rejecting those items.
- Have the source file, a matter/request reference, and the reviewer's name.
- Remember the tool is pre-alpha: use it on synthetic or approved sample documents until your firm
  has validated it for client work.

## 1. Inspect the file

```powershell
legal-redact inspect contract.pdf          # or contract.docx
```

Read every finding. Scans need OCR (PF003/PF015-PF018, and PF019 if you use a cloud OCR engine);
images inside a `.docx` are OCR'd (PF113/PF129/PF131/PF133); refused constructs (encrypted, macros,
tracked changes, unsupported images, large unread images) mean the file cannot be processed safely
as-is — convert or resolve them in Word/PDF first.

## 2. Choose the purpose policy

```powershell
# third-party disclosure / DSAR-style redaction (higher bar before a name is auto-proposed)
legal-redact suggest contract.pdf -o decisions.json --policy dsar-disclosure

# or sharing a contract with a counterparty/bank/adviser
legal-redact suggest contract.pdf -o decisions.json --policy contract-sharing
```

The presets are illustrative; write your own TOML when the matter needs different treatment.
`publication` is the strictest review route.

## 3. Review every decision (rendered page or text review)

```powershell
legal-redact review contract.docx --policy dsar-disclosure --controller "Example Firm LLP" `
  --matter "DSAR-2026-17" --purpose "response to a data-subject access request" `
  --reviewer "A. Lawyer"
```

- Accept or reject **every** suggestion; use "Find repeats" for names that occur elsewhere.
- Acknowledge pre-flight REVIEW findings only after reading them.
- Tick the three **reviewer attestations** (special-category data checked manually, indirect
  identifiers considered, authorised disclosure). Export is blocked until they are ticked.
- Export writes `<output>.evidence.md` next to the redacted file automatically.

## 4. Or do it from the command line

```powershell
legal-redact suggest contract.pdf -o decisions.json --policy dsar-disclosure
# set each decision's status to "accepted" or "rejected" in decisions.json
legal-redact redact contract.pdf --decisions decisions.json -o contract.redacted.pdf `
  --ack PF003,PF016 `
  --controller "Example Firm LLP" --matter "DSAR-2026-17" `
  --purpose "response to a data-subject access request" --reviewer "A. Lawyer" `
  --attest special_categories,indirect_identifiers,authorised `
  --evidence evidence.md --dpia dpia.md
```

Only acknowledge the codes that actually appear and that you have checked. `--ack` is not a
formality: it records that a human looked at what the tool could not decide.

## 5. Quality checks before releasing anything

1. The verifier report says **PASS** and names the checks it ran.
2. Any **WARN V112** (image text not read / too small) means those pixels were **not verified**:
   open the image and check it yourself before releasing.
3. Re-read the output: the requester's own data is present, the third-party data is gone, the
   commercial terms/dates/law survive.
4. Special-category data, indirect identifiers, handwriting and images the OCR did not read are
   **your** manual check — the tool does not cover them comprehensively.
5. A second person samples the output for the matter's risk level.

## 6. Keep the records

- `evidence.md`/`evidence.json`: what was decided, what the verifier proved, hashes of source and
  output, engine/locality, residual risks and your attestations.
- `dpia.md`: the DPIA/ROPA input extract (below).
- The decision file (JSON): text-free; offsets/boxes/categories only.
- The original and the redacted file, retained per your firm's retention policy.
- The rendered preview PDF used by the review page is a temporary file and is deleted when the
  session ends; do not rely on it.

## 7. DPIA / record-of-processing input

```powershell
legal-redact redact contract.pdf --decisions decisions.json -o contract.redacted.pdf `
  --evidence evidence.md --dpia dpia.md
```

`dpia.md` is a one-page input extract: files and hashes, matter/purpose, data categories and volumes,
processing environment, technical measures, residual risks, reviewer attestations, and a checklist
for the controller to complete — legal basis, necessity/proportionality, retention,
recipients/transfers, whether a DPIA is required, DPO advice and sign-off. It is **not** a DPIA.

## What not to do

- Do not treat a PASS as "no personal data remains". It means: what the reviewer chose to remove is
  gone, and no hidden copy was found. Detection is a suggestion layer.
- Do not use a batch `--attest`/`--ack` without reading the findings and the output.
- Do not use cloud OCR for privileged/confidential documents unless the firm has approved the
  service and the transfer is lawful (the tool requires `--allow-cloud-ocr` and a per-file PF019
  acknowledgement, with a written warning).
- Do not call the output anonymous. Redaction is not anonymisation (Recital 26).

Related: [COMPLIANCE.md](COMPLIANCE.md) · [THREAT_MODEL.md](THREAT_MODEL.md) ·
[DOCX.md](DOCX.md) · [TESTING.md](TESTING.md) · [BENCHMARKS.md](BENCHMARKS.md)
