# Holdout v5 data notice: CUAD contract excerpts

`src/legal_redact/bench/indep.py` builds the v5 benchmark suite from 20 short
excerpts of third-party contract text. This file records where that text comes
from, its licence, exactly which excerpts are vendored, their SHA-256 hashes,
and what was changed when they were copied. It is documentation for the same
reason the other held-out suites have a pin: a benchmark is worthless if nobody
can check what is in it.

## Dataset and licence

- **Dataset:** CUAD v1 (Contract Understanding Atticus Dataset).
- **Hugging Face:** <https://huggingface.co/datasets/theatticusproject/cuad/tree/a3c393f5d103fd0c516374e4fdff676c8176dcb1> (revision `a3c393f5d103fd0c516374e4fdff676c8176dcb1`).
- **GitHub:** <https://github.com/TheAtticusProject/cuad> (repository commit `67faa0e6023b04fcaae6cc09497ab00e5d63a2a2`).
- **Licence:** CC BY 4.0, as stated on the CUAD dataset card (`theatticusproject/cuad`, CC BY 4.0). The underlying contracts are exhibits to public US SEC EDGAR filings and are themselves public records.
- **Citation:** Hendrycks, Burns, Chen & Ball, CUAD: An Expert-Annotated NLP Dataset for Legal Contract Review, NeurIPS 2021 (arXiv:2103.06268).
- **Selection:** on 2026-11-06 we downloaded the 100 contract texts in `CUAD_v1/full_contract_txt/Part_I` and screened pages for clean prose (no tables, columns, confidential-redaction markers or personal names). The 20 selected excerpts cover 20 different contract types; each is a short run of complete sentences drawn from one page of the source contract (the dataset flattens a PDF page to one line), and each generated document is exactly one page (2,643-3,219 characters of text). Only the excerpts below are vendored; the dataset is not.

## What is vendored

`src/legal_redact/bench/indep_sources.json` holds exactly 20 cleaned one-page
excerpts (1,776-2,329 characters each; 41,981 characters in total). The module
recomputes each excerpt's SHA-256 when it is imported, so an edited excerpt
cannot silently change the suite.

Normalisation applied to the dataset text (and nothing else):

1. whitespace collapsed (the dataset flattens each source PDF page to one line);
2. typographic quotes and dashes folded to ASCII, non-ASCII characters dropped;
3. hyphenation from PDF line breaks rejoined (`non- exclusive` -> `non-exclusive`);
4. one stray duplicated full stop and one obvious PDF line-break split (`BKC a s required` -> `BKC as required`) fixed;
5. page headers, footers and page numbers are outside the page text in the dataset and are not included.

The selected pages contain no names of individuals, no contact details, no
signatures and no account numbers -- only company names and contract prose.
No contract text was added or invented. The personal-data and payment paragraphs
inserted by `indep.py` are synthetic and are ours, not CUAD's.

### Note on the project's `CONTRIBUTING.md` rule

"No real documents, ever" exists so that no client or matter document can leak
into this repository. These excerpts are not client or matter documents: they are
licensed, already-public third-party contract text, vendored as benchmark input
and attributed here. They are still only a fallback for measuring generalisation:
if CUAD's licence or provenance ever becomes a problem, delete
`indep_sources.json` and fall back to public SEC EDGAR exhibit text with the same
notice structure.

## Per-excerpt provenance

All paths below are under `CUAD_v1/full_contract_txt/Part_I/` at dataset
revision `a3c393f5d103fd0c516374e4fdff676c8176dcb1`.

| id | contract type | source file | page | chars | SHA-256 of vendored excerpt |
|---|---|---|---|---|---|
| `s01` | SERVICES AGREEMENT | `ABILITYINC_06_15_2020-EX-4.25-SERVICES AGREEMENT.txt` | 5 | 2074 | `96bb89c00aead9381689ae29afb295365facc2a8d1604435f94eb996d4478e34` |
| `s02` | DISTRIBUTOR AGREEMENT | `ACCURAYINC_09_01_2010-EX-10.31-DISTRIBUTOR AGREEMENT.txt` | 4 | 2265 | `a2505b41618f0db3c480c2c1eb58e63519877ca2654a0820b5bb412325301ede` |
| `s03` | COLLABORATION AGREEMENT | `ANIXABIOSCIENCESINC_06_09_2020-EX-10.1-COLLABORATION AGREEMENT.txt` | 8 | 1819 | `670ef89e1c9d88e31edb8953978d15fa15dd58281036c26c3d90208f87434b9d` |
| `s04` | OUTSOURCING AGREEMENT | `ASPIRITYHOLDINGSLLC_05_07_2012-EX-10.6-OUTSOURCING AGREEMENT.txt` | 21 | 2137 | `9caa6d7c99ec8defc4204fa809ab659c4aea4e3686d40cf121817700da85b116` |
| `s05` | SPONSORSHIP AGREEMENT | `AlliedEsportsEntertainmentInc_20190815_8-K_EX-10.34_11788308_EX-10.34_Sponsorship Agreement.txt` | 3 | 1776 | `38094892f48c0aeb51593ba8118b702df4eaff1ede19d5f51a314a3f85027e77` |
| `s06` | MANUFACTURING AGREEMENT | `Antares Pharma, Inc. - Manufacturing Agreement.txt` | 95 | 2213 | `a9a9dcdf54550bc58b2758d7b3c05d9d5b53c02b12dd6dc5d3dc97b5f5e3dbe3` |
| `s07` | TRADEMARK LICENSE AGREEMENT | `ArconicRolledProductsCorp_20191217_10-12B_EX-2.7_11923804_EX-2.7_Trademark License Agreement.txt` | 2 | 2227 | `f4221eaab0bb9e7f9fd38c5289da45cdebdb42affb7a517bfdb5aedb457d3ff5` |
| `s08` | MAINTENANCE AGREEMENT | `AtnInternationalInc_20191108_10-Q_EX-10.1_11878541_EX-10.1_Maintenance Agreement.txt` | 107 | 2325 | `e7dcdceb381da5156fc6b35370c424cd76988f9de30b10adc4623e0b247b6627` |
| `s09` | INTELLECTUAL PROPERTY AGREEMENT | `BABCOCK_WILCOXENTERPRISES,INC_08_04_2015-EX-10.17-INTELLECTUAL PROPERTY AGREEMENT between THE BABCOCK _ WILCOX COMPANY and BABCOCK _ WILCOX ENTERPRISES, INC..txt` | 64 | 2193 | `902d9eb57450334905ff47a8574320a483214123732b8396526c3d6cb18e59da` |
| `s10` | AGENCY AGREEMENT | `BONTONSTORESINC_04_20_2018-EX-99.3-AGENCY AGREEMENT.txt` | 34 | 2129 | `f2f3afe6e59fd5c7b218a0162742fd630cf63b0cc2c8dad7bbc2273d6b3df7b0` |
| `s11` | DEVELOPMENT AGREEMENT | `ConformisInc_20191101_10-Q_EX-10.6_11861402_EX-10.6_Development Agreement.txt` | 23 | 1787 | `02fab3b473772234b7c6419d1bd6cdd733428731a8bec83fc1dbfab50116d1d1` |
| `s12` | AFFILIATE AGREEMENT | `CreditcardscomInc_20070810_S-1_EX-10.33_362297_EX-10.33_Affiliate Agreement.txt` | 6 | 2283 | `0628d525f5779de55bf066e4bbf6f3a2fe4248da325035859dbe90901b4e00ee` |
| `s13` | CONTENT LICENSE AGREEMENT | `EuromediaHoldingsCorp_20070215_10SB12G_EX-10.B(01)_525118_EX-10.B(01)_Content License Agreement.txt` | 7 | 2190 | `8c15e66e5c043dda161b129dafed744aa0b6167332c7566de45f5a82eef6e360` |
| `s14` | FRANCHISE AGREEMENT | `INTERNATIONALFASTFOODCORP_04_04_1997-EX-99-FRANCHISE AGREEMENT.txt` | 82 | 1927 | `a578e3b52ca492c522348fd87a767d5a10c264dcc5506cae1efa93dd8c597e85` |
| `s15` | CONSULTING AGREEMENT | `GLOBALTECHNOLOGIESLTD_06_08_2020-EX-10.16-CONSULTING AGREEMENT.txt` | 2 | 1996 | `91d3b6de4b8234acd8f5b5817dc913563321a549a77b1567b5a3b2f8617a5fd2` |
| `s16` | JOINT VENTURE AGREEMENT | `IGENEBIOTECHNOLOGYINC_05_13_2003-EX-1-JOINT VENTURE AGREEMENT.txt` | 20 | 2174 | `846394392d26af8fc2cde2113103523568e1c27f16c405502a0b18bb2435b8dd` |
| `s17` | SOFTWARE HOSTING AGREEMENT | `INKTOMICORP_06_08_1998-EX-10.14-SOFTWARE HOSTING AGREEMENT.txt` | 29 | 2084 | `e7f976ec22d344d3cd513c634e095334d9a3f924cdaa7e20d49f1efda9ceb576` |
| `s18` | STRATEGIC ALLIANCE AGREEMENT | `ENERGOUSCORP_03_16_2017-EX-10.24-STRATEGIC ALLIANCE AGREEMENT.txt` | 17 | 2155 | `d737af4f33f7114540f706fa0b028483d13039a923aa6424f9f584474e918321` |
| `s19` | SUPPLY AGREEMENT | `INTERSECTENT,INC_05_11_2020-EX-10.1-SUPPLY AGREEMENT.txt` | 7 | 2329 | `7f31ae8a07db44538d19b579223eae042fac02545e48dc2f342ac26794e76854` |
| `s20` | TRANSPORTATION CONTRACT | `GRANTIERRAENERGYINC_05_07_2012-EX-10.6-TRANSPORTATION CONTRACT.txt` | 18 | 1898 | `74035253e4b5bc5953521cf6ab9e579bbfaf78d1dd2b75b7e2f3792950886152` |

Download URLs (the `page` column is the index of the flattened page line in
that file; the editable URL is the quoted file name appended to `.../CUAD_v1/full_contract_txt/Part_I/`):

- `s01`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/ABILITYINC_06_15_2020-EX-4.25-SERVICES%20AGREEMENT.txt>
- `s02`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/ACCURAYINC_09_01_2010-EX-10.31-DISTRIBUTOR%20AGREEMENT.txt>
- `s03`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/ANIXABIOSCIENCESINC_06_09_2020-EX-10.1-COLLABORATION%20AGREEMENT.txt>
- `s04`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/ASPIRITYHOLDINGSLLC_05_07_2012-EX-10.6-OUTSOURCING%20AGREEMENT.txt>
- `s05`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/AlliedEsportsEntertainmentInc_20190815_8-K_EX-10.34_11788308_EX-10.34_Sponsorship%20Agreement.txt>
- `s06`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/Antares%20Pharma%2C%20Inc.%20-%20Manufacturing%20Agreement.txt>
- `s07`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/ArconicRolledProductsCorp_20191217_10-12B_EX-2.7_11923804_EX-2.7_Trademark%20License%20Agreement.txt>
- `s08`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/AtnInternationalInc_20191108_10-Q_EX-10.1_11878541_EX-10.1_Maintenance%20Agreement.txt>
- `s09`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/BABCOCK_WILCOXENTERPRISES%2CINC_08_04_2015-EX-10.17-INTELLECTUAL%20PROPERTY%20AGREEMENT%20between%20THE%20BABCOCK%20_%20WILCOX%20COMPANY%20and%20BABCOCK%20_%20WILCOX%20ENTERPRISES%2C%20INC..txt>
- `s10`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/BONTONSTORESINC_04_20_2018-EX-99.3-AGENCY%20AGREEMENT.txt>
- `s11`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/ConformisInc_20191101_10-Q_EX-10.6_11861402_EX-10.6_Development%20Agreement.txt>
- `s12`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/CreditcardscomInc_20070810_S-1_EX-10.33_362297_EX-10.33_Affiliate%20Agreement.txt>
- `s13`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/EuromediaHoldingsCorp_20070215_10SB12G_EX-10.B%2801%29_525118_EX-10.B%2801%29_Content%20License%20Agreement.txt>
- `s14`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/INTERNATIONALFASTFOODCORP_04_04_1997-EX-99-FRANCHISE%20AGREEMENT.txt>
- `s15`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/GLOBALTECHNOLOGIESLTD_06_08_2020-EX-10.16-CONSULTING%20AGREEMENT.txt>
- `s16`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/IGENEBIOTECHNOLOGYINC_05_13_2003-EX-1-JOINT%20VENTURE%20AGREEMENT.txt>
- `s17`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/INKTOMICORP_06_08_1998-EX-10.14-SOFTWARE%20HOSTING%20AGREEMENT.txt>
- `s18`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/ENERGOUSCORP_03_16_2017-EX-10.24-STRATEGIC%20ALLIANCE%20AGREEMENT.txt>
- `s19`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/INTERSECTENT%2CINC_05_11_2020-EX-10.1-SUPPLY%20AGREEMENT.txt>
- `s20`: <https://huggingface.co/datasets/theatticusproject/cuad/resolve/a3c393f5d103fd0c516374e4fdff676c8176dcb1/CUAD_v1/full_contract_txt/Part_I/GRANTIERRAENERGYINC_05_07_2012-EX-10.6-TRANSPORTATION%20CONTRACT.txt>

## Injected data (ours)

- `faker` 40.44.0 (`en_US`), seeded deterministically from the source id and variant; gold values are *not* from CUAD.
- Gold categories, every document: PERSON x3, EMAIL, PHONE, ADDRESS, IBAN, CARD, ID, DOB. Values are invented; emails use the reserved `example.org` / `example.com` / `example.eu` domains and phone numbers use the fictional `+1 ... 555-xxxx` / `+44 20 7946 0xxx` ranges.
- Protected decoys, every document: party/company names x2, amounts x2, dates x2, governing-law string x1, section references x3. They are legally meaningful text that should survive redaction.
- The three inserted paragraphs are spliced between CUAD paragraphs at fixed relative positions; their wording is ours. The excerpt text itself is untouched by the insertion.

## Pin and rules

- **Pin:** `INDEP_SHA256` = `dc6632512e8228def4333e0ead988fee82dd54398368513c62f3f95ea5d18dbb`. It is a SHA-256 over the generated logical text plus labels for all 100 documents, created before any scoring. `tests/test_indep.py` fails if the generator changes.
- **Rules:** nothing is tuned on v5; it is scored once per change set, then spent. It is deliberately not wired into `FROZEN_SHA256`, `HOLDOUT_SHA256`, `HOLDOUT4_SHA256` or any dev-corpus test.
- **Reproducing the pin:** the text/label pin is independent of the PDF renderer, but it does depend on the `faker` version (40.44.0 here; pinned `faker` seeds are only stable within a version). Regenerate with the project's dev extras installed. The rendered one-page PDFs are deterministic on a given PyMuPDF version (2.1 here).
- **Layout:** `indep.py` has its own neutral renderer -- US Letter, Times, 84pt side margins, centred title and a rule -- deliberately different from `synth.py`/`frozen.py`. There are no deliberate hard cases.
