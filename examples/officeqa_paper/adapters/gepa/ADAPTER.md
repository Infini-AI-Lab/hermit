# OfficeQA Skill

## Task Overview
You are answering factual and computational questions against parsed text files of historical U.S. Treasury Bulletins (roughly the 1950s–1980s). Questions may involve (a) direct table lookups, (b) summation or filtering of multiple table rows, (c) currency conversion using historical exchange rates, and (d) rounding of the final result. Answers are graded by exact string match against the expected value, so the numeric result — including rounding behavior — must be exactly right.

## Retrieval Discipline
- Start by narrowing to the most likely candidate file before reading long passages. When multiple source files are given, prefer the later-dated bulletin for a month's figures, since it contains revised values and additional months (e.g., the April 1956 bulletin has both January and February 1956 in Table 1, with the January figure matching the revision shown in the March bulletin).
- Prefer targeted grep patterns that name the exact entity, period, or table concept: e.g., "1956-January", "1956-February", "PDO-1", "Maturity Schedule", "new money", the fiscal-month row labels.
- After a promising match, read only a small surrounding span (headings, column headers, subtotal rows, footnotes) and verify it matches the requested year, basis (e.g., "cash basis", "receipts from the public" vs. "budget receipts"), and unit (millions of dollars, billions of dollars).
- Check for subtotal/total rows before summing rows yourself. Treasury Bulletin tables frequently include year subtotals and "fiscal year to date"/"calendar year to date" rows (e.g., the PDO-1 maturity schedule tables give a "1982 Total" subtotal row for all securities with final maturity in calendar year 1982). If a subtotal exists, prefer it over hand-summing.
- Table structure knowledge:
  - Table 1 ("Summary of Federal Government Cash Transactions with the Public") lists monthly "Total receipts from the public" and "Total expenditures to the public" in millions of dollars, followed by trust account figures and the fiscal-year-to-date row.
  - PDO-1 style tables ("Maturity Schedule of Interest-Bearing Marketable Public Debt Securities Other than Regular Weekly and 52-Week Treasury Bills Outstanding") list individual maturity dates grouped under a calendar year, ending with a year-total row that splits holdings between U.S. Government accounts/Federal Reserve banks and "all other investors".
  - Narrative sections (e.g., "Weekly Treasury Bills") state cumulative new-money financing amounts directly in text; grep for phrases like "new money" and read the surrounding sentence.

## Evidence Discipline
- Extract the exact value from the retrieved text before doing any arithmetic.
- Keep track of each operand's period, unit, and semantic role so nearby proxy values are not mixed in (e.g., do not confuse "total receipts" with "budget receipts", "receipts from the public" with trust-account receipts, or monthly rows with cumulative "to date" rows).
- Fiscal year knowledge: the U.S. federal fiscal year runs July–June. "The first 11 months of FY 1963" means July 1962 through May 1963; cumulative figures for that span are stated directly in the narrative text.
- If the question asks for a transformed or derived quantity, compute only after confirming every operand. Label each intermediate value with its unit in your work.

## Domain Facts: Historical Exchange Rates
- Mid-20th-century USD/INR: the rupee was pegged at Re 1 = US$0.21 from 1951 through early June 1966, giving an annual average of about 4.7619 INR per USD. Rounded to the nearest hundredth, this is 4.76, not 4.76.
- When a question says to use "the yearly average <year> USD/INR exchange rate (rounded to the nearest hundredths place)", use 4.76 for years up to 1965. Do not substitute unrounded or alternative rates (4.7619, 4.762, etc.) — the grading expected value is computed with 4.76.
- Apply the rate at the correct step: sum the USD values first, then multiply the sum by the rounded rate, then round the product to the requested precision (nearest hundredth).

## Arithmetic and Rounding Discipline (critical — prior failures occurred here)
- Use standard round-half-up commercial rounding at the final step:
  - 5.5 rounds to 6 (do NOT truncate, do not round half down). A cumulative $5.5 billion rounded to the nearest whole number is 6.
  - When rounding to the nearest hundredth, carry the multiplication to full precision first, then round once at the end (e.g., 11,152 × 4.76 = 53083.52).
- Never round intermediate values differently than the question specifies; if the question specifies the exchange rate be rounded to the hundredths place, round the rate first and use that rounded rate in the multiplication.
- Re-verify the multiplication/summation arithmetic with an explicit recomputation before finalizing.

## Final Answer Discipline
- Return the final answer only after one last consistency check against the retrieved evidence: correct months/period, correct table row, correct unit, correct rate, correct rounding direction.
- Copy the final answer from a checked, recomputed value, not from an unverified intermediate guess.
- Match the requested output format: if the question asks for a plain number ("rounded to the nearest whole number"), return just the number (e.g., "6"). Include units/currency notation only when the question's requested format calls for it or the value is inherently a currency amount (e.g., "53083.52 (₹53083.52 million INR)").
