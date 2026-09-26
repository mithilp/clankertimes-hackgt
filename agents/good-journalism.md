# Good journalism: what a records-driven scoop looks like

From 14 widely read or prize-listed original stories (2025-2026) and reporter tip sheets. `playbook.md` covers how to verify; this file covers what is worth verifying and how to write it.

## 1. Worked examples

Format: **new fact** / **record** / **hook** / **lede**.

1. **Valere charter superintendent paid $870,000 (ProPublica + Texas Tribune, Mar 2025).** New fact: a superintendent of fewer than 1,000 students took home up to $870,714, likely the most of any Texas superintendent. Record: the network's IRS Form 990s vs. his employment letter showing $285,887 base pay; bonuses were left out of the stated salary. Hook: outlier with an anchor (more than NYC's chancellor). Lede: pay vs. enrollment, in the headline.
2. **Kristi Noem's undisclosed $80,000 (ProPublica, Jul 2025).** New fact: a dark-money group paid her LLC $80,000 that never appeared on her ethics disclosures. Record: the group's 990 (a 10% fee on $800,000 raised), the Delaware LLC registration (paid minutes after formation), her federal disclosure. Hook: named official, number, and the form that should have listed it. Lede: payment, then omission.
3. **Trump's own mortgages (ProPublica, Dec 2025).** New fact: in 1993 he signed two mortgages seven weeks apart, each pledging a different Palm Beach house as his principal residence, then rented them out. Record: the mortgage documents. Hook: his administration's own fraud standard (used against Lisa Cook) fits him. Lede: that standard, applied to his records.
4. **FDA let banned factories keep shipping drugs (ProPublica, Jun-Aug 2025; Pulitzer finalist).** New fact: 150+ drugs or ingredients entered the U.S. from factories under import bans, via quiet exemptions, for over a dozen years. Record: FDA import alerts plus the exemption lists. Hook: a ban that was not a ban. Lede: official status vs. practice.
5. **Texas sepsis after the abortion ban (ProPublica, Feb 2025).** New fact: sepsis for second-trimester pregnancy loss rose 50%+ after the ban. Record: state hospital discharge (billing) data, 2017-2023, with a published methodology. Hook: a measured policy consequence. Lede: before/after rate, then one case.
6. **Connecticut towing, "On the Hook" (CT Mirror + ProPublica; 2026 Pulitzer, Local).** New fact: tow companies sold cars within 15 days, one of the shortest windows nationally, while routinely under-valuing them. Record: 6,000+ DMV forms from 2022-23, obtained by records request. Hook: a legal practice that strips people of their cars. Lede: the fastest-in-the-nation clock.
7. **"Burned" (San Francisco Chronicle; 2026 Pulitzer, Explanatory).** New fact: major insurers set coverage using one flawed valuation tool (360Value), leaving fire victims underinsured. Record: court cases (CourtLink), insurer manuals and claim files. Hook: legal, fully covered, still can't rebuild. Lede: a homeowner, then the tool behind the gap.
8. **Baltimore students and late buses (Baltimore Banner; 2026 Pulitzer finalist).** New fact: about 1 in 4 public buses carrying 25,000+ students arrive late or not at all in the morning. Record: a records-request database of student tract and school, joined to real-time bus locations. Hook: a daily failure nobody had measured. Lede: the ratio.
9. **Brightline Florida bonds (Bond Buyer, WLRN, Commercial Observer; Feb-Mar 2026).** New fact: 3.1 million riders and $214 million revenue in 2025, against projections of 6.6 million and $697 million; the auditor flagged going-concern doubt. Record: audited financials and event notices posted to EMMA. Hook: promise vs. result. Lede: projection, then actual.
10. **World Liberty's quiet token sales (Bloomberg, May 2026).** New fact: after $550 million+ in public rounds, it privately sold 5.9 billion more tokens and grew the founders' allocation, adding about $660 million to the Trump family's fortune. Record: on-chain data and governance filings, confirmed by the company. Hook: insiders cashed out while early buyers stayed locked. Lede: undisclosed sale, then who gained.
11. **Polymarket bets before the Iran strikes (NYT, Mar 2026; Bloomberg follow-up).** New fact: at least 16 accounts betting on the strike timing made $100,000+ each; most were new and bet only on Iran markets. Record: Polymarket's public on-chain trade history. Hook: apparent trading on secret military timing. Lede: account count and profit, then timing.
12. **SoFi's "sale" that was a loan (Muddy Waters short report, Mar 2026; allegations, disputed by SoFi).** Claimed fact: a $312 million transaction booked as a loan sale looks like borrowing. Record: 10-K/10-Q loan-sale notes contradicted by Utah UCC lien filings. Hook: the profit behind management's pay may not exist. Lede: one dollar figure, two records that disagree.
13. **First Brands' hidden debt (Bloomberg, FT, WSJ; Sep-Oct 2025).** New fact: over $5 billion of its roughly $11 billion debt was off balance sheet, including $2.3 billion of factoring, and some invoices were allegedly financed more than once. Record: Chapter 11 filings, including a forensic review. Hook: private credit's hidden leverage. Lede: reported vs. real debt.
14. **Data brokers hiding opt-out pages (The Markup + CalMatters, Aug 2025).** New fact: 35 of 499 registered brokers used code that hid their legally required deletion pages from search engines. Record: California's data broker registry, then each site's robots tags. Hook: a legal duty quietly defeated. Lede: count and mechanism. Nine+ companies removed the code after contact.

## 2. Scoop vs. rehash

A scoop has at least one of these, and a news search shows nobody has said it yet:
- **A number nobody computed.** Summed, ranked, or compared ($870,000 total, 1-in-4 ratio, 50% rise).
- **Two records that contradict each other.** 990 vs. employment letter, 10-K vs. UCC liens, ethics form vs. LLC payment, residence pledge vs. rental use. The most reliable pattern.
- **Promise vs. actual.** An official statement or projection vs. audited results (Brightline).
- **An official status that isn't true in practice.** "Banned" factories still shipping; "required" opt-out pages that are hidden.
- **The accuser meets their own standard.**
- **A connection nobody drew.** The same person or entity in two unrelated datasets (donor and vendor; LLC and payer; wallet and announcement).

Rehash: the central fact comes from a press release, a lawsuit, an already-covered watchdog report, or "amid scrutiny" framing. Context on someone else's scoop is not a scoop; credit it and find the next fact.

## 3. Record types that reliably yield scoops

| Record | Where | Pattern that signals a story |
|---|---|---|
| Insider trades (Form 4) + 10b5-1 plan disclosures | sec.gov/edgar/search; sec.gov/data-research/sec-markets-data/insider-transactions-data-sets | Clustered insider sales, or a 10b5-1 plan adopted weeks earlier, before a negative 8-K (guidance cut, restatement, FDA rejection, lost customer) |
| 8-K events and late filings | EDGAR full-text search | Item 4.01 auditor resignation, Item 4.02 non-reliance, NT 10-K late notice, CFO leaving within 90 days of an auditor change |
| Related-party transactions (DEF 14A, 10-K notes) | EDGAR + state business registries | Payments to an entity whose officer or registered agent is an executive or relative; amounts growing yearly |
| Accounting claims vs. lien records | EDGAR + state UCC search (secretary of state sites) | A "sale" of assets while a UCC lien still names the buyer as secured party; receivables pledged to several lenders |
| BDC / private-credit marks | 10-Q schedules of investments (EDGAR) | Same loan marked differently across BDCs (e.g., 91 vs. 77); marks near par while the borrower defaults elsewhere |
| SPAC / de-SPAC | S-4, DEFM14A, later 10-Ks | Redemptions above 90%; sponsor promote vs. cash delivered; merger-deck projections vs. first-year actuals; going-concern language within 12 months |
| Municipal bonds | emma.msrb.org | Going-concern opinion; draw on a debt service reserve; forbearance or trustee-change notice; late continuing-disclosure filings; official statement projections vs. actuals; underwriter fees above peers |
| Public pensions | ACFRs on EMMA and pension sites; board minutes | Fees reported net or missing (PE carry); benchmark changed the year returns fell; placement agents or managers tied to board members |
| Banks | FFIEC call reports cdr.ffiec.gov/public; enforcement at orders.fdic.gov and apps.occ.gov/EASearch | CRE above 300% of capital; fast growth in brokered or uninsured deposits; consent orders missing from the holding company's filings |
| Fintech and money transmitters | CFPB complaints at consumerfinance.gov/data-research/consumer-complaints; nmlsconsumeraccess.org | Complaint spike for one company; state consent orders; operating where its license lapsed |
| Crypto and prediction markets | Etherscan, Polygonscan, DAO governance forums and Snapshot | New wallet, one-market concentration, low-odds entry, hours before news; token allocations changed in governance posts; insider wallets selling during a lockup |
| Nonprofits | projects.propublica.org/nonprofits (990s) | Schedule J pay far above the published salary; Schedule L related-party deals; payments to an official's LLC |
| Officials' finances | OGE 278e (oge.gov); House and Senate PTRs (disclosures-clerk.house.gov, efdsearch.senate.gov); state ethics boards | Income missing that another record shows paid; trades just before committee action; PTRs filed after the 45-day deadline |
| Campaign finance and lobbying | fec.gov/data, state portals, lda.senate.gov, efile.fara.gov | A donor or lobbying client wins a contract, a grant, or regulatory relief within months |
| Procurement | usaspending.gov, sam.gov, city and county council agendas | Single bidder; vendor formed shortly before the award; modifications doubling the value; vendor officers who are donors |
| Courts and bankruptcy | courtlistener.com (RECAP), PACER, claims agents (Kroll, Stretto, Epiq) | First-day declarations and examiner reports showing undisclosed debt or payments; creditor lists naming public bodies |
| Inspections and registries | FDA import alerts (datadashboard.fda.gov), OSHA, echo.epa.gov, state registries, DMV forms via records request | Repeat violators still operating or paid; exemptions quietly granted; a legal duty vs. what the company actually does |

## 4. Headline and lede rules

- **One fact, one number, one named accountable party.** Who did what, how much, per what record.
- **Name the record as the authority.** "Tax filings show," "bond disclosures show." The reader should know it is checkable.
- **Give the number an anchor.** Peers, the official's own claim, the projection, or before/after (3.1M riders vs. a projected 6.6M).
- **Put the contradiction in one sentence.** Stated X, record shows Y.
- **Headline = the new fact, not the topic.** "Brightline carried half its projected riders," not "Brightline faces debt questions."
- **Second paragraph: why it matters, plus the response.** What the rule requires; the party's reply or "did not respond."
- **Method box.** One paragraph: which records, which years, how counted. ProPublica and The Markup routinely publish one; it is what makes an AI-written finding credible.
- **Allegations stay allegations.** A short report or lawsuit is a claim: say whose, and give the denial.

## 5. What makes a legal story interesting

Legal stories land when they have one of the section 2 shapes plus one of these:
- **Outlier rank.** Highest, fastest, only, first (the shortest tow-sale window in the country).
- **Mechanism reveal.** One tool, rule, or loophole explains many harms (360Value, FDA exemptions, robots tags).
- **Hidden money flows.** Who got paid, and how much, when nobody knew they were paid at all.
- **Ordinary-person stakes.** Car, house, drugs, school commute. Readers care when it could be them.
- **Quiet change.** An allocation, rule, or benchmark changed without announcement (governance posts, board minutes, amended filings).

## Sources

- https://www.propublica.org/article/propublica-most-read-stories-2025
- https://www.propublica.org/article/valere-public-schools-superintendent-salary-texas
- https://www.propublica.org/article/kristi-noem-political-donations-income-dark-money-dhs-ethics
- https://www.mprnews.org/story/2025/12/08/propublica-trumps-own-mortgages-match-his-description-of-mortgage-fraud-records-reveal
- https://www.propublica.org/article/fda-drugs-banned-foreign-factories-list
- https://www.propublica.org/article/texas-abortion-ban-sepsis-maternal-mortality-analysis
- https://www.propublica.org/article/texas-maternal-mortality-analysis-methodology
- https://ctmirror.org/2025/12/15/ct-mirror-propublica-towing-investigation-explainer/
- https://www.poynter.org/business-work/2026/san-francisco-chronicle-wins-pulitzer-california-wildfires/
- https://www.thebanner.com/education/k-12-schools/baltimore-city-students-mta-EN4OO6SETNHUHGQETMHGKWTFZA/
- https://www.bondbuyer.com/news/brightline-florida-raises-going-concern-warning-lacks-liquidity-for-debt-payments
- https://commercialobserver.com/2026/02/brightline-florida-ridership-revenue-debt-finance-distress/
- https://www.bloomberg.com/news/articles/2026-05-12/quiet-token-sales-boosted-trump-crypto-wealth-by-660-million
- https://www.bloomberg.com/trump-crypto-reporting
- https://www.bloomberg.com/graphics/2026-polymarket-traders-who-knew-the-future/
- https://www.npr.org/2026/04/10/nx-s1-5780569/betting-polymarket-iran-investigation-lawmakers
- https://muddywatersresearch.com/research/2026/mw-sofi-borrowingsales-0330/
- https://www.cnbc.com/2025/10/10/first-brands-implosion-lenders-scramble-to-contain-the-fallout-.html
- https://www.americanbar.org/groups/business_law/resources/business-law-today/2026-february/first-brands-avoid-being-two-timed-by-collateral/
- https://themarkup.org/privacy/2025/08/12/we-caught-companies-making-it-harder-to-delete-your-data
- https://www.propublica.org/article/trump-administration-financial-disclosures-steve-feinberg
- https://www.poynter.org/reporting-editing/2026/2026-pulitzer-prize-winners-list/
- https://www.bostonfed.org/publications/current-policy-perspectives/2026/early-warnings-private-credit-bdc-portfolios.aspx
- https://icapital.com/insights/private-credit/the-valuation-gap-how-timing-mismatches-are-shaping-private-credit-risk-perception/
- Tip sheets: https://journalistsresource.org/economics/municipal-bonds-munis-finance-tip-sheet/ ; https://journalistsresource.org/home/sec-filings-cover-companies/ ; https://journalistsresource.org/economics/nonprofit-questions-answers-tips/ ; https://gijn.org/stories/researching-government-contracts-for-covid-19-spending-a-gijn-factsheet/ ; https://www.open-contracting.org/wp-content/uploads/2024/12/OCP2024-RedFlagProcurement-1.pdf ; https://ewa.org/data-research-tips/how-to-report-on-school-and-college-finances-using-the-electronic-municipal-market-access-emma-database
