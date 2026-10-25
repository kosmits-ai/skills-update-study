# peek_census.py - quick look at the census while it is still running.
# Reads the version lists already saved to disk; sends no requests.
import json, glob, collections

files = glob.glob("data/full/skills/*/*/versions.json")
counts = {}
for f in files:
    data = json.load(open(f))
    owner, slug = f.split("/")[-3], f.split("/")[-2]
    counts[f"{owner}/{slug}"] = len(data["versions"])

values = list(counts.values())

# Group version counts into buckets, like the final census report.
def bucket(n):
    return ("0" if n == 0 else "1" if n == 1 else "2" if n == 2 else
            "3-4" if n <= 4 else "5-9" if n <= 9 else "10-19" if n <= 19 else "20+")

hist = collections.Counter(bucket(n) for n in values)
order = ["0", "1", "2", "3-4", "5-9", "10-19", "20+"]

print(f"Skills checked so far: {len(values)}")
print("Versions per skill:", {k: hist.get(k, 0) for k in order})
print(f"Skills with >= 5 versions: {sum(n >= 5 for n in values)} "
      f"({100 * sum(n >= 5 for n in values) / max(1, len(values)):.1f}%)")
print("\nTop 10 most-updated skills:")
for name, n in sorted(counts.items(), key=lambda x: -x[1])[:10]:
    print(f"  {n:4d}  {name}")