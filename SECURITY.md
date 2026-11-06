# Security policy

legal-redact handles confidential documents, so security reports are welcome.

## Reporting

Please report vulnerabilities privately, not in a public issue.

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose
**Report a vulnerability**. If that option is unavailable, open a minimal public issue asking for a
private channel, and include no document content, exploit details or other confidential material in
it.

This is a pre-alpha project maintained on a best-effort basis. There is no bug bounty.

## What counts

- A way to make an export retain content the user chose to redact, in a way the verifier reports as
  a pass. (Highest priority: this defeats the tool's purpose.)
- A way for the review server to expose document content to another process, web page or host.
- Document text appearing in a report, log or decision file.
- Path handling that writes outside the intended output file.

## Known limits (not vulnerabilities)

These are documented and listed in every verification report: text rendered as images or outlines,
text under images (no OCR), unusual font encodings, digital signatures, and indirect identification.
Detector misses (personal data not suggested) are accuracy issues, not security issues; please open
a normal issue with a synthetic example.

## Scope and support

Pre-alpha software, developed on synthetic data, with no stability promise. Do not rely on it as the
only safeguard before disclosing real documents. See `docs/THREAT_MODEL.md`.
