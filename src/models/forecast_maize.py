"""Forecast maize prices (KES per kg) 1-3 months ahead and compare with baselines.

Run from the project root:
    python src/models/forecast_maize.py

Reads  data/processed/prices_monthly.csv, coverage_clean.csv
Writes reports/model_summary.csv         accuracy by horizon and model
       reports/model_by_series.csv       accuracy by series, horizon and model
       reports/forecasts_latest.csv      next 1-3 month forecasts and risk flag
       reports/maize_history.csv         monthly history used (for the dashboard)
"""
from pathlib import Path

import numpy as np
import pandas as pd
from xgboost import XGBRegressor

ROOT = Path(__file__).resolve().parents[2]
PROC = ROOT / "data" / "processed"
REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)

HORIZONS = [1, 2, 3]
TEST_START = pd.Period("2024-09", freq="M")   # last ~2 years are held out for testing
HIGH, MEDIUM = 1.20, 1.10                      # draft risk thresholds (see README)

monthly = pd.read_csv(PROC / "prices_monthly.csv")
cov = pd.read_csv(PROC / "coverage_clean.csv")

# ------------------------------------------------ choose usable maize series
active = cov[(cov["crop"] == "maize") & (cov["months_with_data"] >= 60)
             & (cov["completeness"] >= 0.8) & (cov["last"] >= "2026-01")]
print("Series used (maize, 60+ months, 80%+ complete, still reporting in 2026):")
print(active.to_string(index=False))

# ------------------------------------------------------------ build panel
frames, history = [], []
for r in active.itertuples():
    s = monthly[(monthly["county"] == r.county) & (monthly["crop"] == "maize")
                & (monthly["pricetype"] == r.pricetype)].copy()
    s.index = pd.PeriodIndex(s["month"], freq="M")
    s = s["price_kg"].sort_index()
    name = f"{r.county} ({r.pricetype.lower()})"
    history.append(pd.DataFrame({"series": name, "month": s.index.astype(str), "price_kg": s.values}))
    s = s.reindex(pd.period_range(s.index.min(), s.index.max(), freq="M"))
    s = s.interpolate(limit=3, limit_area="inside")      # fill gaps of up to 3 months
    p, lp = s, np.log(s)
    f = pd.DataFrame(index=s.index)
    f["series"] = name
    f["price"] = p
    f["logp"] = lp
    for k in (1, 2, 3, 6, 12):
        f[f"ret_{k}"] = lp - lp.shift(k)
    f["dev_12"] = lp - np.log(p.rolling(12, min_periods=9).mean())
    f["vol_6"] = lp.diff().rolling(6, min_periods=4).std()
    f["m_sin"] = np.sin(2 * np.pi * s.index.month / 12)
    f["m_cos"] = np.cos(2 * np.pi * s.index.month / 12)
    for h in HORIZONS:
        f[f"actual_{h}"] = p.shift(-h)
        f[f"persist_{h}"] = p                    # baseline 1: price stays the same
        f[f"seasonal_{h}"] = p.shift(12 - h)     # baseline 2: same month last year
        f[f"target_{h}"] = lp.shift(-h) - lp     # what XGBoost learns: log change
    f.index.name = "origin"
    frames.append(f.reset_index())
panel = pd.concat(frames, ignore_index=True)
pd.concat(history).to_csv(REPORTS / "maize_history.csv", index=False)

dummies = pd.get_dummies(panel["series"], prefix="s", dtype=float)
FEATURES = (["logp", "ret_1", "ret_2", "ret_3", "ret_6", "ret_12", "dev_12", "vol_6", "m_sin", "m_cos"]
            + list(dummies.columns))
X = pd.concat([panel, dummies], axis=1)


def new_model():
    return XGBRegressor(n_estimators=300, max_depth=3, learning_rate=0.05, subsample=0.8,
                        colsample_bytree=0.8, random_state=0)


def metrics(y, yhat):
    err = y - yhat
    return {"MAE": float(np.mean(np.abs(err))), "RMSE": float(np.sqrt(np.mean(err ** 2))),
            "MAPE_%": float(np.mean(np.abs(err) / y) * 100), "n": int(len(y))}


# ------------------------------------------------------ evaluate on the test period
rows = []
for h in HORIZONS:
    d = X[X[f"target_{h}"].notna() & X[f"actual_{h}"].notna()]
    train = d[d["origin"] < TEST_START - h]            # target month is before the test period
    test = d[d["origin"] >= TEST_START].dropna(subset=[f"seasonal_{h}"])
    model = new_model().fit(train[FEATURES], train[f"target_{h}"])
    test = test.assign(xgb=test["price"] * np.exp(model.predict(test[FEATURES])))
    for label, col in [("Persistence (no change)", f"persist_{h}"),
                       ("Seasonal naive (same month last year)", f"seasonal_{h}"),
                       ("XGBoost", "xgb")]:
        for series, g in test.groupby("series"):
            rows.append({"horizon_months": h, "model": label, "series": series,
                         **metrics(g[f"actual_{h}"].values, g[col].values)})
        rows.append({"horizon_months": h, "model": label, "series": "ALL",
                     **metrics(test[f"actual_{h}"].values, test[col].values)})
res = pd.DataFrame(rows)
res.round(3).to_csv(REPORTS / "model_by_series.csv", index=False)
summary = res[res["series"] == "ALL"].drop(columns="series")
summary.round(3).to_csv(REPORTS / "model_summary.csv", index=False)

print(f"\nTest period starts {TEST_START}. Accuracy over all series (lower is better):")
print(summary.round(2).to_string(index=False))
print()
for h in HORIZONS:
    s = summary[summary["horizon_months"] == h].set_index("model")["MAE"]
    best_base = min(s["Persistence (no change)"], s["Seasonal naive (same month last year)"])
    verdict = "BEATS" if s["XGBoost"] < best_base else "does NOT beat"
    print(f"{h}-month ahead: XGBoost MAE {s['XGBoost']:.2f} {verdict} the best baseline ({best_base:.2f})")

# ------------------------------------ final model on all data -> latest forecasts
latest = X.sort_values("origin").groupby("series").tail(1).set_index("series")
out = pd.DataFrame(index=latest.index)
out["last_obs_month"] = latest["origin"].astype(str)
out["last_price_kg"] = latest["price"]
for h in HORIZONS:
    d = X[X[f"target_{h}"].notna()]
    model = new_model().fit(d[FEATURES], d[f"target_{h}"])
    out[f"forecast_{h}m"] = latest["price"] * np.exp(model.predict(latest[FEATURES]))
trail = {s: g.sort_values("origin")["price"].tail(12).mean() for s, g in panel.groupby("series")}
out["trailing_12m_mean"] = pd.Series(trail)
out["ratio_to_12m_mean"] = out[[f"forecast_{h}m" for h in HORIZONS]].max(axis=1) / out["trailing_12m_mean"]
out["risk"] = np.where(out["ratio_to_12m_mean"] >= HIGH, "HIGH",
                       np.where(out["ratio_to_12m_mean"] >= MEDIUM, "MEDIUM", "LOW"))
out.round(3).reset_index().to_csv(REPORTS / "forecasts_latest.csv", index=False)
print("\nLatest forecasts (KES per kg) and draft risk flag:")
print(out.round(2).to_string())
print(f"\nSaved results to {REPORTS}")