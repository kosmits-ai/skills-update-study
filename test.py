import zipfile, glob, collections
zips = glob.glob("data/full/skills/*/*/zips/*.zip")
names = collections.Counter()
for z in zips:
    try:
        with zipfile.ZipFile(z) as zf:
            names.update({n.rsplit("/", 1)[-1].lower() for n in zf.namelist()})
    except zipfile.BadZipFile:
        pass
print(len(zips), "zips")
for n, c in names.most_common(15):
    print(f"{c:6d}  {c/len(zips):5.1%}  {n}")