"""Clean the WFP Kenya food prices into a monthly county table.

Run from the project root:
    python src/ingest/clean_prices.py

Reads  data/raw/wfp_food_prices_ken.csv
Writes data/processed/prices_monthly.csv   (county x crop x price type x month)
       data/processed/coverage_clean.csv   (months of data per series)
"""
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw" / "wfp_food_prices_ken.csv"
OUT = ROOT / "data" / "processed"
OUT.mkdir(parents=True, exist_ok=True)

# Some admin2 values are old districts or sub-counties: map them to counties.
COUNTY_FIX = {"Moyale": "Marsabit", "Ijara": "Garissa",
              "Meru North": "Meru", "Meru South": "Meru"}

# WFP renamed commodities over time: group the names into one crop each.
CROPS = {
    "maize": ["Maize", "Maize (white)", "Maize (white, dry)"],
    "beans": ["Beans", "Beans (dry)"],
    "rice": ["Rice", "Rice (aromatic)"],
}
NAME_TO_CROP = {name: crop for crop, names in CROPS.items() for name in names}

# ---------------------------------------------------------------- load
df = pd.read_csv(RAW, dtype=str)
df = df[~df["date"].str.startswith("#", na=False)].copy()
df["date"] = pd.to_datetime(df["date"], errors="coerce")
df["price"] = pd.to_numeric(df["price"], errors="coerce")
print(f"Rows loaded: {len(df):,}")

df = df.dropna(subset=["date", "price", "admin2"])
df = df[df["price"] > 0]
df["county"] = df["admin2"].replace(COUNTY_FIX)
df["crop"] = df["commodity"].map(NAME_TO_CROP)
df = df.dropna(subset=["crop"]).copy()
print(f"Maize, beans and rice rows after dropping bad rows: {len(df):,}")


# ------------------------------------------------- convert to KES per kg
def kg_per_unit(unit):
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*KG\s*$", str(unit), flags=re.I)
    if m:
        return float(m.group(1))
    return 1.0 if str(unit).strip().upper() == "KG" else float("nan")


df["kg"] = df["unit"].map(kg_per_unit)
bad_units = df.loc[df["kg"].isna(), "unit"].value_counts()
if len(bad_units):
    print("Dropping rows with unrecognised units:")
    print(bad_units.to_string())
df = df.dropna(subset=["kg"]).copy()
df["price_kg"] = df["price"] / df["kg"]
df["month"] = df["date"].dt.to_period("M")

# ------------------------------------------------------------------ QC
# The same market and month can appear twice under different names/units,
# for example "Maize" per KG and "Maize (white)" per 90 KG.
key = ["month", "market", "crop", "pricetype"]
g = df.groupby(key)["price_kg"]
spread = (g.max() / g.min())[g.size() > 1]
print(f"\nMarket-months reported more than once: {len(spread):,}")
if len(spread):
    print("Highest / lowest kg-price within those (1.00 = identical):")
    print(spread.describe().round(3).to_string())

# ------------------------- one value per market-month, then county-month
mkt = df.groupby(key + ["county"], as_index=False)["price_kg"].mean()
monthly = (mkt.groupby(["county", "crop", "pricetype", "month"], as_index=False)
              .agg(price_kg=("price_kg", "mean"), n_markets=("market", "nunique")))
monthly["month"] = monthly["month"].astype(str)
monthly.to_csv(OUT / "prices_monthly.csv", index=False)

# ------------------------------------------------------------ coverage
rows = []
for (county, crop, ptype), grp in monthly.groupby(["county", "crop", "pricetype"]):
    m = pd.PeriodIndex(grp["month"], freq="M")
    span = (m.max() - m.min()).n + 1
    rows.append({"county": county, "crop": crop, "pricetype": ptype,
                 "months_with_data": len(m), "span_months": span,
                 "completeness": round(len(m) / span, 2),
                 "first": str(m.min()), "last": str(m.max())})
cov = pd.DataFrame(rows).sort_values(["crop", "pricetype", "months_with_data"],
                                     ascending=[True, True, False])
cov.to_csv(OUT / "coverage_clean.csv", index=False)

good = cov[(cov["months_with_data"] >= 60) & (cov["completeness"] >= 0.8)]
print(f"\nSeries (county x crop x price type): {len(cov)}")
print(f"Series with at least 60 months and 80% completeness: {len(good)}")
print()
print(good.to_string(index=False))
print(f"\nSaved: {OUT / 'prices_monthly.csv'}")
print(f"Saved: {OUT / 'coverage_clean.csv'}")