"""Regenerate data.js (and update market.js) for the Compounder Screen.

Pulls ten years of fundamentals for every US filer from the SEC EDGAR XBRL
"frames" API, screens them, and writes the shortlist the dashboard reads.
Requests go through edgartools, which sends your SEC identity and retries.

Run from this folder, with the fork installed (pip install -e .. from here):

    python refresh_snapshot.py --identity "Your Name you@example.com"

Prices are not on EDGAR. Existing prices in market.js are kept; new companies
get a ticker and no price, so add one in the dashboard's Admin tab.
"""
import argparse
import datetime as dt
import json
import re
import statistics
import time
from pathlib import Path

from edgar import set_identity
from edgar.httprequests import download_json

HERE = Path(__file__).resolve().parent
FRAMES = "https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/{unit}/CY{year}{suffix}.json"
TICKERS = "https://www.sec.gov/files/company_tickers.json"

# Tags fetched per calendar year. "I" tags are balance-sheet values at year end.
DURATION = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
            "OperatingIncomeLoss", "NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic",
            "ProfitLoss", "NetCashProvidedByUsedInOperatingActivities",
            "PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
            "PaymentsToAcquireOtherPropertyPlantAndEquipment"]
INSTANT = ["StockholdersEquity", "LongTermDebt", "LongTermDebtNoncurrent", "LongTermDebtCurrent",
           "LongTermDebtAndCapitalLeaseObligations", "LongTermDebtAndCapitalLeaseObligationsCurrent",
           "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
           "DebtLongtermAndShorttermCombinedAmount", "DebtInstrumentCarryingAmount", "SeniorNotes",
           "LongTermNotesPayable", "CashAndCashEquivalentsAtCarryingValue",
           "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]
SHARES = "WeightedAverageNumberOfDilutedSharesOutstanding"


def fetch_frames(years, pause):
    """Return {tag: {year: {cik: value}}} and {cik: entity name}."""
    data, names = {}, {}
    jobs = [(t, "USD", "") for t in DURATION] + [(t, "USD", "Q4I") for t in INSTANT] + [(SHARES, "shares", "")]
    for year in years:
        for tag, unit, suffix in jobs:
            url = FRAMES.format(tag=tag, unit=unit, year=year, suffix=suffix)
            try:
                rows = download_json(url)["data"]
            except Exception as exc:  # a frame that does not exist returns 404
                print(f"  skipped {tag} {year}: {type(exc).__name__}")
                rows = []
            by_cik = data.setdefault(tag, {}).setdefault(year, {})
            for row in rows:
                by_cik[row["cik"]] = row["val"]
                names[row["cik"]] = row["entityName"]
            time.sleep(pause)
        print(f"fetched {year}")
    return data, names


def first(values):
    return next((v for v in values if v is not None), None)


def build_series(data, years, cik):
    """Assemble one company's yearly series, choosing among alternative tags."""
    def get(tag, year):
        return data.get(tag, {}).get(year, {}).get(cik)

    s = {k: [] for k in ("rev", "op", "ni", "cfo", "capex", "eq", "debt", "cash", "sh")}
    for y in years:
        s["rev"].append(first([get("Revenues", y), get("RevenueFromContractWithCustomerExcludingAssessedTax", y),
                               get("SalesRevenueNet", y)]))
        s["op"].append(get("OperatingIncomeLoss", y))
        ni = get("NetIncomeLoss", y)
        if ni is None or abs(ni) < 1e5:  # some filers tag this in millions; fall back
            ni = first([get("NetIncomeLossAvailableToCommonStockholdersBasic", y), get("ProfitLoss", y), ni])
        s["ni"].append(ni)
        s["cfo"].append(get("NetCashProvidedByUsedInOperatingActivities", y))
        s["capex"].append(first([get("PaymentsToAcquirePropertyPlantAndEquipment", y),
                                 get("PaymentsToAcquireProductiveAssets", y),
                                 get("PaymentsToAcquireOtherPropertyPlantAndEquipment", y)]))
        s["eq"].append(get("StockholdersEquity", y))
        nc, lease = get("LongTermDebtNoncurrent", y), get("LongTermDebtAndCapitalLeaseObligations", y)
        candidates = [get("LongTermDebt", y),
                      None if nc is None else nc + (get("LongTermDebtCurrent", y) or 0),
                      get("LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities", y),
                      None if lease is None else lease + (get("LongTermDebtAndCapitalLeaseObligationsCurrent", y) or 0),
                      get("DebtLongtermAndShorttermCombinedAmount", y), get("DebtInstrumentCarryingAmount", y),
                      get("SeniorNotes", y), get("LongTermNotesPayable", y)]
        found = [v for v in candidates if v is not None]
        s["debt"].append(max(found) if found else None)  # largest tag is closest to total debt
        s["cash"].append(first([get("CashAndCashEquivalentsAtCarryingValue", y),
                                get("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", y)]))
        s["sh"].append(get(SHARES, y))
    return s


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def first_pass_score(s, min_revenue, tax=0.21):
    """Return (latest index, score) or None when the company fails the screen."""
    n = len(s["rev"])
    latest = next((i for i in (n - 1, n - 2) if s["rev"][i] is not None and s["op"][i] is not None), None)
    if latest is None or s["rev"][latest] < min_revenue:
        return None
    if any(sum(v is not None for v in s[k]) < 8 for k in ("rev", "op", "ni", "cfo", "eq")):
        return None
    margins = [o / r for o, r in zip(s["op"], s["rev"]) if o is not None and r]
    if min(margins) <= 0:
        return None
    roics = []
    for i in range(n):
        if s["op"][i] is None or s["eq"][i] is None:
            continue
        capital = s["eq"][i] + (s["debt"][i] or 0) - (s["cash"][i] or 0)
        if capital > 0:
            roics.append(s["op"][i] * (1 - tax) / capital)
    margin = statistics.median(margins)
    quality = clamp(statistics.median(roics), 0, 0.5) / 0.5 if len(roics) >= 5 else clamp(margin, 0, 0.35) / 0.35
    start = next(i for i, v in enumerate(s["rev"]) if v is not None)
    growth = (s["rev"][latest] / s["rev"][start]) ** (1 / (latest - start)) - 1 if latest > start else 0
    fcf = sum(c - (x or 0) for c, x, p in zip(s["cfo"], s["capex"], s["ni"]) if c is not None and p and p > 0)
    profit = sum(p for c, p in zip(s["cfo"], s["ni"]) if c is not None and p and p > 0)
    conversion = fcf / profit if profit > 0 else 0
    score = (0.4 * quality + 0.2 * clamp(margin, 0, 0.4) / 0.4 + 0.2 * clamp(growth, 0, 0.2) / 0.2
             + 0.2 * clamp(conversion, 0, 1.2) / 1.2)
    return latest, score


def millions(v):
    return None if v is None else round(v / 1e6)


def write_data_js(path, years, picked, screened, names):
    lines = []
    for cik, latest, s in picked:
        row = {"c": cik, "n": names[cik], "L": years[latest]}
        for k in ("rev", "op", "ni", "cfo", "capex", "eq", "debt", "cash"):
            row[k] = [millions(v) for v in s[k]]
        row["sh"] = [None if v is None else round(v / 1e5) / 10 for v in s["sh"]]
        lines.append(json.dumps(row, separators=(",", ":"), ensure_ascii=False))
    head = ("// Fundamentals snapshot from the SEC EDGAR XBRL frames API (data.sec.gov). "
            "USD millions; sh = diluted weighted shares, millions.\n")
    body = (f'window.FUND={{asOf:"{dt.date.today().isoformat()}",years:{json.dumps(years, separators=(",", ":"))},'
            f"screened:{screened},co:[\n" + ",\n".join(lines) + "\n]};\n")
    path.write_text(head + body, encoding="utf-8")


ENTRY = re.compile(r'^(\d+):\{t:"([^"]*)",n:"([^"]*)",s:"([^"]*)",px:([\d.]+|null)\},?$')


def update_market_js(path, picked, names):
    """Keep known tickers, sectors and prices; add tickers for new companies."""
    known, header = {}, []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = ENTRY.match(line.strip())
            if m:
                known[int(m.group(1))] = m.groups()[1:]
            elif not known and line.strip() != "}};":
                header.append(line)
    if not header:
        header = ["// Tickers, display names, sectors and last prices, keyed by SEC CIK.",
                  'window.MKT={asOf:"' + dt.date.today().isoformat() + '",src:"manual entry",co:{']
    new = [cik for cik, _, _ in picked if cik not in known]
    tickers = {}
    if new:
        for item in download_json(TICKERS).values():
            tickers.setdefault(int(item["cik_str"]), item["ticker"])  # first listed is the primary ticker
    rows = []
    for cik, _, _ in picked:
        if cik in known:
            t, n, sector, px = known[cik]
        else:
            t, n, sector, px = tickers.get(cik, f"CIK{cik}"), names[cik].title().replace('"', ""), "", "null"
            print(f"  new company: {t} ({n}); no price yet")
        rows.append(f'{cik}:{{t:"{t}",n:"{n}",s:"{sector}",px:{px}}}')
    path.write_text("\n".join(header) + "\n" + ",\n".join(rows) + "\n}};\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--identity", required=True, help='Your name and email, as the SEC requires: "Jane Doe jane@example.com"')
    ap.add_argument("--end-year", type=int, default=dt.date.today().year - 1, help="Last fiscal year to include")
    ap.add_argument("--top", type=int, default=45, help="How many companies to shortlist")
    ap.add_argument("--min-revenue", type=float, default=5e9, help="Minimum latest revenue in dollars")
    ap.add_argument("--exclude-cik", type=int, nargs="*", default=[1341439],
                    help="CIKs to leave out. Default drops Oracle, whose debt tags are incomplete")
    ap.add_argument("--pause", type=float, default=0.12, help="Seconds between requests")
    args = ap.parse_args()

    set_identity(args.identity)
    years = list(range(args.end_year - 9, args.end_year + 1))
    data, names = fetch_frames(years, args.pause)

    recent = set()
    for tag in ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"):
        for y in years[-2:]:
            recent.update(data.get(tag, {}).get(y, {}))
    ranked = []
    for cik in recent:
        s = build_series(data, years, cik)
        result = first_pass_score(s, args.min_revenue)
        if result:
            ranked.append((result[1], cik, result[0], s))
    ranked.sort(key=lambda r: -r[0])
    picked = [(cik, latest, s) for _, cik, latest, s in ranked[:args.top] if cik not in set(args.exclude_cik)]

    write_data_js(HERE / "data.js", years, picked, len(ranked), names)
    update_market_js(HERE / "market.js", picked, names)
    print(f"wrote {len(picked)} companies (from {len(ranked)} that passed the screen) to data.js and market.js")


if __name__ == "__main__":
    main()
