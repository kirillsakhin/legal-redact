# Compliance scope: what this tool does and does not do

This note is written for a legal audience. It maps the tool to GDPR-relevant *redaction tasks* and
states explicitly what it does **not** certify. It is not legal advice, and the tool does not decide
whether any processing or disclosure is lawful.

**One-line description to use:** *legal-redact is a GDPR-aware, compliance-supporting redaction
workflow: it finds candidate personal data, a lawyer decides, the export really removes the content,
an independent verifier withholds the file if anything was missed, and the workflow writes a record
of what was checked and decided. It is not a GDPR compliance certificate and does not anonymise
documents.*

## What the tool is for

It helps with the file-level part of tasks such as:

| Task / GDPR context | What the tool does | What it does not do |
|---|---|---|
| Data minimisation (Art. 5(1)(c)) when sharing or disclosing a document | Policy-driven redaction of eight personal-data categories (person, e-mail, phone, address, ID, IBAN, card, date of birth), true removal from the file, metadata/hidden-carrier scrubbing, independent verification of the export | It cannot decide which data is adequate/relevant/non-excessive in the matter; no legal-basis (Art. 6) decision; no minimisation judgement beyond the chosen policy |
| Redacting third-party data from a disclosure or data-subject response (Art. 15 context) | `dsar-disclosure` preset, review gate for every suggestion, retain/keep decisions by a human, export evidence | It cannot tell the requester's own data from other people's; no DSR intake, identity verification, exemption analysis, deadline or response workflow |
| Redacting personal data from copies at the end of retention / an erasure request (Art. 17 context) | Redacts personal data from a document copy and proves the copy is clean | It does not delete source systems, e-mails, backups, logs or other copies; no proof of erasure, no retention schedule |
| Security-of-processing context (Art. 32) | Local and offline by default; fail-closed verifier; no telemetry; cloud OCR only as an explicit, disclaimed opt-in; review gate limits automation | It is not a security programme: no encryption at rest, access control, key management, audit log, or organisational measures |
| Accountability context (Art. 5(2)) | `redact --evidence report.md` (or `.json`) writes an evidence report: decisions taken, verifier checks, hashes, tool version, OCR engine/locality, no-network statement | No Article 30 records of processing, no DPIA (Art. 35), no DPO/roles, no regulator-ready audit trail beyond that report |

## What the tool does not do (read this before calling it "compliance")

- **It does not anonymise.** Removing names can leave context that identifies a person (a rare job
  title, a family relation, a small population, a unique amount and date). Recital 26 / Art. 4(1)
  require a contextual assessment; the tool cannot make it. Redaction is removal from the file, not
  anonymisation, and not pseudonymisation either.
- **It does not determine what is personal data.** Detection is a suggestion layer for structured
  identifiers and English-language names/addresses. Context, indirect identifiers, and
  company-vs-person ambiguity are human calls.
- **It does not cover Art. 9 special categories comprehensively.** Health, genetic, biometric,
  religion, sexual orientation, criminal-offence data are not detected as categories; a human must
  check for them.
- **It does not detect indirect identifiers or singling-out.** Rare facts, combinations of
  quasi-identifiers, free-text narrative and handwriting are outside the detectors.
- **It does not provide a legal basis, lawful-basis assessment, DPIA, records of processing,
  processor agreement, transfer assessment, breach notification, or DSR workflow.**
- **It does not produce a compliance certificate.** No tool can: compliance depends on the
  controller's legal analysis and organisational measures.

## The human review gate is the control, not decoration

Every suggestion needs an accept/reject decision, every pre-flight review finding needs explicit
acknowledgement, and the export is blocked otherwise. A missed name released with a human
"accept-all" is a human decision made on a suggestion list; the tool's verifier only proves that
what the reviewer chose to remove is gone — it cannot prove the reviewer chose everything that
should be removed. Plan your review process accordingly (second-person check, sampling, or a
checklist for context-dependent data).

## Reviewer attestations (v0.5.1)

Before exporting from the review page, the reviewer must tick three statements (and the CLI can
record the same with `--attest`): special-category data checked manually, indirect identifiers
considered, and authority to make the disclosure. They are the reviewer's own statements — the tool
does not verify them and they do not make the output compliant. Their purpose is to make the human
steps explicit and to record them in the evidence report, so the file shows what a reviewer
attested to, not just that a button was pressed.

## The evidence report: what it records (and what it does not prove)

`legal-redact redact … --evidence report.md` (or a `.json` path) writes, after a successful export:

- tool version, Python/platform, UTC timestamp, policy name;
- source and redacted file paths, sizes and SHA-256;
- the decision record: counts by status and accepted-by-category, manual additions, acknowledged
  pre-flight findings;
- the OCR engine (name, version, local/cloud, cloud approval) when OCR was used;
- every verifier check (id, status, detail) and the verifier's own out-of-scope list;
- the no-network statement and the "not a compliance certificate" disclaimer.

A companion `--dpia report.md` flag writes a **one-page DPIA/ROPA input extract** from the same
record: files and hashes, matter/purpose, data categories and volumes, processing environment,
technical measures, residual risks, attestations, and a checklist for the controller (legal basis,
necessity/proportionality, retention, recipients/transfers, DPIA decision, DPO advice, sign-off).
The extract is explicitly not a DPIA. A step-by-step disclosure/DSAR workflow using the presets,
attestations, evidence and DPIA extract is in [docs/DSAR_WORKFLOW.md](DSAR_WORKFLOW.md).

It also contains, since v0.5.1: the per-matter context you supply (`--controller`, `--matter`,
`--purpose`, `--reviewer`), the reviewer attestations that were given or the explicit "NOT RECORDED"
marker, a **residual-risk section** (verifier warnings such as unverified image pixels, acknowledged
pre-flight findings, and the categories the detectors do not cover: special-category data, indirect
identifiers, handwriting/stamps), and a **DPIA/ROPA inputs table** with purpose, data categories,
volumes, processing environment, technical measures and residual risks — the fields the workflow can
supply, with retention, transfers and legal basis left for the controller to complete.

It deliberately contains no document text. It proves what the tool checked and what the reviewer
decided; it does not prove that detection was complete, that the file is anonymous, or that the
disclosure is lawful. Attach it to the matter file as a record of the workflow, not as a
certificate.

## Which preset for which purpose

| Preset | Purpose |
|---|---|
| `contract-sharing` | Share a contract with a counterparty, bank or adviser: personal identifiers out, commercial terms and dates in |
| `dsar-disclosure` | Disclosure/DSAR-style redaction: higher bar before a name is auto-proposed, reviewer keeps the requester's own data manually |
| `publication` | Publishing or training material: strictest review, nearly every lone name reviewed |
| `counterparty`, `auditor` | Earlier illustrative examples; kept for the published benchmarks and manual experiments |

All are illustrative, not legal advice; write your own `--policy` TOML for your matter (the schema
is in `src/legal_redact/policies/*.toml`, and every category must have an action).

## Data flow and security claims we can support

- Default: everything happens on the local machine; no network calls from the package, no telemetry.
- Scanned PDFs use a local OCR engine by default. A custom/cloud OCR engine is **opt-in per
  invocation** (`--allow-cloud-ocr`) and **per file** (the PF019 disclaimer must be acknowledged);
  a non-local engine is refused before any page is rendered. The disclaimer states that cloud
  processing can compromise the document.
- The verifier reads exports with different libraries than the writer and withholds a file when it
  finds a leak or hidden residue. It is not a guarantee against every conceivable carrier; the
  threat model lists what is out of scope (`docs/THREAT_MODEL.md`).
- AGPL-3.0: internal use is permitted; redistribution and network-service use carry the licence
  obligations.

## How to describe it (and how not to)

| Say | Do not say |
|---|---|
| "GDPR-aware redaction workflow with human review and verifiable removal" | "GDPR-compliant redaction" |
| "Helps with data-minimisation and disclosure redaction tasks" | "Anonymises documents" / "makes documents anonymous" |
| "Suggested personal data; a lawyer decides" | "Automatically finds and removes all personal data" |
| "Independent verification that selected content was removed" | "Certified / guarantees no personal data remains" |
| "Local by default; cloud OCR is an explicit opt-in with a confidentiality warning" | "Your documents never leave your machine" (false once cloud OCR is enabled) |

The published benchmarks are synthetic and independent-data experiments with their own caveats
(`docs/BENCHMARKS.md`); they are not evidence about a particular client document or regulator's
expectations.
