# Compounder Screen

A single-page dashboard that ranks large US companies as long-term compounders: high returns on capital, steady margins, growth, cash conversion, a sound balance sheet and a price below a simple DCF value. It is a research screen, not investment advice.

## Run it

Open `index.html` in a browser. There is no build step and no server.

## Files

- `index.html`: the app (dashboard, company file, admin settings).
- `data.js`: ten years of fundamentals for 43 companies, taken from the SEC EDGAR XBRL frames API on 2026-11-16. USD millions.
- `market.js`: tickers, display names, sectors and last prices on 2026-11-16.
- `refresh_snapshot.py`: regenerates `data.js` from SEC EDGAR through edgartools.

## Refresh the fundamentals

Needs Python 3.11 or later. From this folder:

```
pip install -e ..
python refresh_snapshot.py --identity "Your Name you@example.com"
```

The SEC requires a name and email on every request. The script makes about 250 requests and takes a few minutes. It rewrites `data.js`, keeps the prices already in `market.js`, and adds a ticker with no price for any company new to the shortlist. Run it after 10-K season, when a new fiscal year is on file. Use `--help` for the options.

The script picks revenue tags in a fixed order rather than by hand, so a fresh run can differ slightly from the first snapshot. It has been tested against a stand-in for the SEC feed, not yet against the live service.

## Admin settings

Open the Admin tab. Settings are saved in your browser.

- Scoring weights for quality, growth, cash generation, balance sheet and valuation.
- Valuation assumptions: discount rate, terminal growth, growth cap, tax rate, required safety margin.
- Universe: minimum revenue, watchlist, exclusions.
- Data: price provider and API key (Financial Modeling Prep or Alpha Vantage), a Refresh prices button, and manual price overrides.

## How the shortlist was built

283 US filers passed a first screen: latest revenue above $5 billion, at least eight years of data and no operating loss since 2016. The top 45 by a first-pass quality rank were kept, and two were removed because their filing tags gave incomplete figures. Banks and insurers drop out because they do not report operating income.

## Known limits

- Figures come from XBRL tags, which companies apply inconsistently. Debt and capital spending are sometimes missing; the app flags each case on the company file.
- Market value uses diluted shares from the latest fiscal year, so a later stock split or buyback skews it.
- Fundamentals do not refresh on their own. `data.js` is a snapshot until you run `refresh_snapshot.py`.
- The DCF is deliberately simple. Treat the safety margin as a way to sort, not as a price target.
