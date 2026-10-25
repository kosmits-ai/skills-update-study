import pandas as pd

u = pd.read_csv("data/full/updates.csv")
u = u[u.change_type != "unknown"]
rank = {"ok": 0, "note": 1, "concern": 2}
dims = ["purpose_capability", "instruction_scope", "install_mechanism",
        "environment_proportionality", "persistence_privilege"]

def direction(s):
    """Count rating changes going up (more risk) and down (less risk) in one update."""
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

# 1) Noise floor: identical content, same model -> any change is scanner noise
same = u[u.same_scan_model == True]
print("Same-model updates:", len(same), "of", len(u))
print(same.groupby("identical")[["scan_changed", "any_dim_change"]].mean().round(3))

# 2) Net direction by what changed
g = u[u.same_scan_model == True].groupby("change_type")[["up", "down"]].sum()
g["n"] = u[u.same_scan_model == True].change_type.value_counts()
g["net_per_update"] = ((g.up - g.down) / g.n).round(3)
print(g.sort_values("net_per_update", ascending=False))

# 3) Model changes between versions
print(u.same_scan_model.value_counts(dropna=False))

v = pd.read_csv("data/full/versions.csv")
print(pd.to_datetime(v.groupby(v.scan_llm_model.isna()).scan_checked_at.median(), unit="ms"))

import pandas as pd
v = pd.read_csv("data/full/versions.csv")
v = v[v.scan_verdict.isin(["clean", "suspicious"])]
v["scan_month"] = pd.to_datetime(v.scan_checked_at, unit="ms").dt.to_period("M")
v["model_recorded"] = v.scan_llm_model.notna()
print(v.groupby(["scan_month", "model_recorded"]).scan_verdict
       .apply(lambda s: round((s == "suspicious").mean(), 3)).unstack())
print(v.groupby("model_recorded")[["scan_vt_status", "scan_skillspector_status"]].count())