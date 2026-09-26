# Beat: Markets

## Focus

Public companies and the people who run them, seen through what they are legally required to file. The SEC's EDGAR system is the beat's primary record: 8-Ks (what just happened), Form 4s (insiders buying and selling), 10-K/10-Q risk factors and legal proceedings, proxy statements (pay, related-party deals), auditor changes and restatements, SPACs and de-SPACs, and short-seller reports that point at filings. We are watching for the moment a company's filings say something different from what it tells the public, and for money moving between insiders and the company.

## What "interesting" means on this beat

- **Insiders ahead of news.** Form 4 sales in the weeks before an 8-K discloses bad news (a failed trial, a lost contract, a subpoena, a restatement). The timing is checkable from filing dates.
- **The filing says what the press release didn't.** A subpoena, SEC or DOJ inquiry, going-concern doubt, material weakness, or covenant breach disclosed deep in a 10-Q while the earnings release was upbeat.
- **Auditors and executives leaving.** 8-K Item 4.01 (auditor changed) and 4.02 (past financials can't be relied on), a CFO who leaves days before a filing deadline, a late-filing notice (NT 10-K).
- **Related-party money.** Proxy disclosures of payments to executives' family firms, aircraft use, loans, leases from the CEO.
- **SPACs and small caps.** Redemptions above 90 percent, sponsors extending deadlines with trust money, de-SPACs that collapse, reverse splits to dodge delisting.
- **AI and crypto companies' filings** against their public claims: revenue recognition, customer concentration, GPU financing, token treasuries.
- **Companies with a Georgia headquarters** get extra weight (the newsroom is in Atlanta).

## What to skip

- Stock-price moves, analyst upgrades, earnings beats and misses on their own.
- Routine 8-Ks: debt offerings by large investment-grade issuers, securitization trusts (auto loan, credit card trusts), dividend declarations, routine director elections.
- Retail-investor hype threads (r/wallstreetbets, stock Twitter) unless many posters point at the same specific filing.
- Short reports with no filing or record behind the claim.

## Where the records usually are

- SEC EDGAR full-text search (the `sec_filings` tool): phrases such as "subpoena", "Wells notice", "going concern", "material weakness", "restatement", "non-reliance", "related party", "resigned", with forms 8-K, 10-Q, 10-K, 4, DEF 14A, NT 10-K
- EDGAR company filing lists (sec.gov/cgi-bin/browse-edgar?company=NAME)
- SEC litigation releases and administrative proceedings (sec.gov/litigation), FINRA disciplinary actions
- Federal court dockets for securities class actions (CourtListener)
- The Federal Register for SEC rules and exemptive orders

## Searching this beat

- Use `sec_filings` with one exact phrase and a form type; the title shows what the 8-K reports. Asset-backed trusts (auto receivables, card master trusts) swamp broad phrases: add a distinctive word.
- Pair every news lead with a filing lookup: a news story that says a company "disclosed" something should have an EDGAR document behind it.

## Where to look

Starting points, not limits:

- `sec_filings` for "subpoena | 8-K", "Wells notice | 10-Q", "going concern | 8-K", "non-reliance | 8-K", "resigned | 8-K", "material weakness | 10-Q"
- Google News for "SEC filing shows", "disclosed in a filing", "short seller report", "restatement", "auditor resigns"
- Feeds: SEC press releases https://www.sec.gov/news/pressreleases.rss and litigation releases https://www.sec.gov/enforcement-litigation/litigation-releases/rss
- r/SecurityAnalysis and r/stocks, only as leads
