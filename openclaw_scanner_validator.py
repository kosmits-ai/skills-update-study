"""
openclaw_scanner_validator.py - Can we trust ClawHub's scan verdicts as a signal?

A trust check on the scanner. If two consecutive versions have identical content
and were judged by the same model, any change in the verdict is scanner noise.
That noise level is the baseline any real update has to beat.

Run from the project root:  python openclaw_scanner_validator.py   (needs pandas)
"""
import pandas as pd

u = pd.read_csv("data/full/tables/consecutive_sampled_versions.csv")
u = u[u.change_type != "unknown"]  # one portion was never downloaded, so it cannot be compared

# The scanner rates each dimension ok < note < concern.
rank = {"ok": 0, "note": 1, "concern": 2}


def direction(s):
    """Count rating changes going up (more risk) and down (less risk) in one update.
    Input looks like "instruction_scope:note->concern;install_mechanism:concern->ok"."""
    up = down = 0
    for part in str(s).split(";"):
        if ":" in part and "->" in part:
            a, b = part.split(":", 1)[1].split("->")
            if a in rank and b in rank:
                up += rank[b] > rank[a]
                down += rank[b] < rank[a]
    return pd.Series({"up": up, "down": down})


u[["up", "down"]] = u.scan_dim_changes.fillna("").apply(direction)
u["any_dim_change"] = u.scan_dim_changes.notna() & (u.scan_dim_changes != "")
u["identical"] = u.change_type == "identical"

# Only same-model pairs: otherwise a verdict change may just be a newer model.
same = u[u.same_scan_model == True]

# 1) Noise floor: the "identical = True" row is pure scanner noise;
#    the "False" row only means something if it is clearly higher.
print("Same-model updates:", len(same), "of", len(u))
print(same.groupby("identical")[["scan_changed", "any_dim_change"]].mean().round(3))

# 2) Which kind of change pushes the scanner towards more risk?
#    net_per_update > 0: ratings went up more often than down.
g = same.groupby("change_type")[["up", "down"]].sum()
g["n"] = same.change_type.value_counts()
g["net_per_update"] = ((g.up - g.down) / g.n).round(3)
print(g.sort_values("net_per_update", ascending=False))

# 3) How often the model differs between the two versions (NaN = model not recorded).
print(u.same_scan_model.value_counts(dropna=False))

# 4) Per version: is a missing model name just a sign of an older scan?
v = pd.read_csv("data/full/tables/sampled_skill_versions.csv")
print(pd.to_datetime(v.groupby(v.scan_llm_model.isna()).scan_checked_at.median(), unit="ms"))

# Share of "suspicious" per scan month. A sudden jump points to a scanner
# change, not to developers behaving differently.
v = v[v.scan_verdict.isin(["clean", "suspicious"])]
v["scan_month"] = pd.to_datetime(v.scan_checked_at, unit="ms").dt.to_period("M")
v["model_recorded"] = v.scan_llm_model.notna()
print(v.groupby(["scan_month", "model_recorded"]).scan_verdict
       .apply(lambda s: round((s == "suspicious").mean(), 3)).unstack())

# Do versions without a recorded model also lack the other scanners' results?
print(v.groupby("model_recorded")[["scan_vt_status", "scan_skillspector_status"]].count())
