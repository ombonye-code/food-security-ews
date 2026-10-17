"""Data-quality check of the WFP Kenya food prices file.

Run from the project root:
    python src/ingest/inspect_prices.py

Reads  data/raw/wfp_food_prices_ken.csv
Writes data/processed/data_quality_report.txt
       data/processed/coverage_county_commodity.csv
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RAW = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "raw" / "wfp_food_prices_ken.csv"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "data" / "processed"
OUT.mkdir(parents=True, exist_ok=True)
FOCUS = "maize|beans|rice"

lines = []


def say(text=""):
    print(text)
    lines.append(str(text))


def section(title):
    say()
    say("=" * 70)
    say(title)
    say("=" * 70)


# ---------------------------------------------------------------- load
df = pd.read_csv(RAW, dtype=str)
# some WFP files carry a second row of "#tags" instead of data: drop it
df = df[~df["date"].str.startswith("#", na=False)].copy()
df["date"] = pd.to_datetime(df["date"], errors="coerce")
for col in ["price", "usdprice", "latitude", "longitude"]:
    df[col] = pd.to_numeric(df[col], errors="coerce")
df["month"] = df["date"].dt.to_period("M")
df["year"] = df["date"].dt.year

# ------------------------------------------------------------ overview
section("1. OVERVIEW")
say(f"File:            {RAW.name}")
say(f"Rows:            {len(df):,}")
say(f"Date range:      {df['date'].min():%Y-%m-%d} to {df['date'].max():%Y-%m-%d}")
say(f"Distinct months: {df['month'].nunique()}")
say(f"Unparseable dates: {df['date'].isna().sum()}")
say()
say("Missing values per column:")
say(df.isna().sum().to_string())

# ----------------------------------------------------------- geography
section("2. GEOGRAPHY")
say(f"Regions (admin1):  {df['admin1'].nunique()}")
say(f"Counties (admin2): {df['admin2'].nunique()}  (check these look like Kenyan counties)")
say(f"Markets:           {df['market'].nunique()}")
say()
per_county = (df.groupby("admin2")
                .agg(markets=("market", "nunique"), rows=("price", "size"),
                     first=("date", "min"), last=("date", "max"))
                .sort_values("rows", ascending=False))
say(per_county.to_string())

# --------------------------------------------------------- commodities
section("3. COMMODITIES, PRICE TYPES, UNITS")
say("Top commodities by number of rows:")
say(df["commodity"].value_counts().head(25).to_string())
say()
say("Price type:")
say(df["pricetype"].value_counts(dropna=False).to_string())
say()
say("Price flag (actual / aggregate / forecast):")
say(df["priceflag"].value_counts(dropna=False).to_string())
say()
say("Currency:")
say(df["currency"].value_counts(dropna=False).to_string())

# ----------------------------------------------------- focus commodity
section("4. FOCUS COMMODITIES (maize, beans, rice)")
focus = df[df["commodity"].str.contains(FOCUS, case=False, na=False)].copy()
say(f"Focus rows: {len(focus):,} of {len(df):,}")
say()
by_item = (focus.groupby(["commodity", "pricetype", "unit"])
                .agg(rows=("price", "size"), markets=("market", "nunique"),
                     counties=("admin2", "nunique"),
                     first=("date", "min"), last=("date", "max"))
                .sort_values("rows", ascending=False))
say(by_item.to_string())
say()
say("NOTE: units differ (for example 90 KG bag vs KG). Prices must be")
say("converted to a common unit (KES per KG) before modelling.")

section("5. ROWS PER YEAR (focus commodities)")
say(pd.crosstab(focus["year"], focus["commodity"]).to_string())

# -------------------------------------------------- coverage by series
section("6. COVERAGE BY COUNTY / COMMODITY / PRICE TYPE")
rows = []
for (county, commodity, ptype), g in focus.groupby(["admin2", "commodity", "pricetype"]):
    months = g["month"].dropna().unique()
    if len(months) == 0:
        continue
    span = (max(months) - min(months)).n + 1
    rows.append({"county": county, "commodity": commodity, "pricetype": ptype,
                 "months_with_data": len(months), "span_months": span,
                 "completeness": round(len(months) / span, 2),
                 "first": str(min(months)), "last": str(max(months))})
cov = pd.DataFrame(rows).sort_values(["commodity", "months_with_data"], ascending=[True, False])
cov.to_csv(OUT / "coverage_county_commodity.csv", index=False)
say(f"Series (county x commodity x price type): {len(cov)}")
usable = cov[(cov["months_with_data"] >= 36) & (cov["completeness"] >= 0.8)]
say(f"Series with at least 36 months and 80% completeness: {len(usable)}")
say()
say("Usable series per commodity and price type:")
say(usable.groupby(["commodity", "pricetype"]).size().to_string())
say()
say("Top 15 series by months of data:")
say(cov.head(15).to_string(index=False))

latest = df["month"].max()
recent = focus[focus["month"] >= latest - 11]
say()
say(f"Counties with focus-commodity data in the last 12 months (to {latest}): "
    f"{recent['admin2'].nunique()}")

# --------------------------------------------------------------- save
(OUT / "data_quality_report.txt").write_text("\n".join(lines), encoding="utf-8")
say()
say(f"Saved: {OUT / 'data_quality_report.txt'}")
say(f"Saved: {OUT / 'coverage_county_commodity.csv'}")
