# DueWise

DueWise is a local website for importing statements, finding recurring payments, and comparing cash-flow scenarios in CAD. It runs locally in a browser and adapts to narrow screens.

## Run locally

On macOS or Linux, from this project folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m app.web
```

If dependencies are already installed, run `.venv/bin/python -m app.web`. On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1` and use `python` for the remaining commands. Open [DueWise on localhost](http://127.0.0.1:5077) and select **Open demo workspace** to explore fictional records immediately. The demo loads the bundled CSV into its own workspace and adds sample bills.

DueWise defaults to port 5077 because macOS Control Center can occupy port 5000. Set `DUEWISE_PORT` to use another port or `DUEWISE_DATA_DIR` to use a different storage folder. The default `instance/` folder holds the login database, session key, and one SQLite file per workspace. Private workspaces persist across sign-in. Signing out of a demo removes that demo workspace. Settings include transaction and workspace deletion. The Flask server binds to loopback for a local demonstration.

## Try the complete workflow

1. **Overview** shows gross purchase totals, category shares, a six-month spending trend, and upcoming confirmed commitments. Use the month and account filters.
2. **Imports** accepts multiple PDF and CSV statements. Download the two fictional PDFs and upload them together to **Everyday checking** in the demo. The September internet payment is already in the demo and should be flagged as a possible duplicate. Open a row to adjust its date, amount, type, account, or category. Correct or exclude incomplete rows, then import the reviewed batch.
3. **Recurring bills** displays likely patterns with source descriptions, cost range, and payment intervals. Review **Phone Plan**, choose its first upcoming date, and confirm it. That creates a tracked bill and records its supporting payments. Use **Add a bill** for a missing commitment. Open a bill to edit its terms, record or link a payment, or archive it.
4. **Cash flow** opens with a seeded example. The baseline deposits a paycheque before rent is due. The alternative delays it by seven days, producing a shortfall. Adjust income or expenses, cancel a bill, or simulate a bill increase. Compare lowest and ending balances, shortfall dates, and the daily CSV export.
5. **Settings** manages account scopes and keyword categories. Transaction-level overrides take precedence over keyword rules.

## Statement formats and limits

| Format | Supported layout |
| --- | --- |
| CSV | UTF-8 with named date and description columns and either a signed amount or separate debit and credit columns. Comma, semicolon, and tab delimiters work. Headers such as `Posted Date`, `Merchant`, and `Withdrawal` are recognized. |
| Text PDF | A table headed `Date / Description / Amount` or `Date / Description / Debit / Credit`. Each transaction occupies one line and includes the year. Separate debit/credit examples use a dash in the unused column. |
| Dates and signs | Prefer `YYYY-MM-DD`. Numeric month-first dates with a four-digit year and English abbreviated months with a year also work. Purchases are negative, while deposits and refunds are positive. |

Upload up to 8 files per batch, 4 MB per file, and 20 MB per request. A PDF can contain up to 35 pages. A batch can contain up to 1,500 rows. If a file fails, its error appears alongside reviewable rows from the other files. Image scans, password-protected PDFs, yearless dates, wrapped transactions, and running-balance layouts require a supported export or manual preparation. Drafts store extracted rows and their file/line references. The app discards the original statements after processing the upload.

Within an account, the date, normalized description, and cents determine potential duplicates. Reviewers can keep a legitimate repeated charge. Spending counts purchase debits and excludes posted transfers, card repayments, income, and refunds. Check the types during review, as keywords cannot identify every bank's descriptions. A month with no imported records leaves a gap on the spending chart. Partial statements can make a month's totals incomplete.

## Detection and projections

Detection groups purchases by account and normalized merchant. It removes common payment terms and explicitly marked reference numbers, then checks interval consistency, amount variation, and the share of the merchant's purchases that fit the cadence. Two suitably spaced occurrences can suggest monthly or yearly payments. Weekly, biweekly, and quarterly patterns require at least three. Confirmed bill terms are editable and recorded payments preserve their original amounts.

A forecast represents one selected chequing, savings, or cash account. It starts from a user-entered balance and uses active bills for that account plus planned movements. Enter credit card repayments as cash-account outflows. Monthly schedules keep their anchor day where possible and clamp to the last day of shorter months. Balances are calculated in cents, and same-day movements are netted. The minimum includes the opening balance. Historical transactions are the evidence for detection and are not carried into a forward projection automatically.

The implementation builds on the existing Flask and SQLite prototype. Python's CSV and decimal libraries handle statement validation and money, pdfplumber and PDFium extract text, and browser-native charts render without external assets.

## Verification and initial evaluation

```bash
python -m pytest -q
python -m app.evaluate
```

The included labelled set has 40 fictional rows, 39 unique transactions, and six labelled recurring series. The baseline flags at least two distinct purchases with the exact same description and amount.

| Method | Precision | Recall | True positives | False positives | Misses |
| --- | ---: | ---: | ---: | ---: | ---: |
| Exact match | 50.0% | 33.3% | 2 | 2 | 4 |
| DueWise | 83.3% | 83.3% | 5 | 1 | 1 |

DueWise flagged two regularly spaced uniform purchases as a recurring bill and missed a utility with large swings in amount. Treat this set as a reproducible development check. The automated suite includes simultaneous PDF/CSV import, duplicate handling, bill edits and history, workspace access and deletion, delayed income, bill changes, and month-end dates.
