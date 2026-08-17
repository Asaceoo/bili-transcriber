import os, sys, subprocess, tempfile

PY = "C:/Users/iamly/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
ANACONDA = "C:/Users/iamly/Anaconda3"
EXE = "D:/bilibili/dist/bili-transcriber-single-0.1.8.exe"

work = tempfile.mkdtemp(prefix="onefile_diag_")
print("extract workdir:", work)
rc = subprocess.run([PY, "-m", "pyinstxtractor_ng", EXE], cwd=work,
                    capture_output=True, text=True)
print("extract rc:", rc.returncode)
print(rc.stdout[-800:])

ext_root = None
for name in os.listdir(work):
    p = os.path.join(work, name)
    if os.path.isdir(p) and name.endswith("_extracted"):
        ext_root = p
        break
print("extracted root:", ext_root)

bundled = set()
if ext_root:
    for dp, _, fns in os.walk(ext_root):
        for f in fns:
            if f.lower().endswith(".dll"):
                bundled.add(f.lower())
print("bundled dll count:", len(bundled))

import pefile
def imports(path):
    out = set()
    try:
        pe = pefile.PE(path, fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
        if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
            for ent in pe.DIRECTORY_ENTRY_IMPORT:
                out.add(ent.dll.decode().lower())
    except Exception as e:
        print("ERR parsing", path, e)
    return out

py314 = os.path.join(ANACONDA, "python314.dll")
ctypes_pyd = os.path.join(ANACONDA, "DLLs", "_ctypes.pyd")
imp_py = imports(py314)
imp_ct = imports(ctypes_pyd)
all_imp = imp_py | imp_ct
print("\n=== MISSING (imported but not bundled) ===")
missing = sorted(all_imp - bundled)
for m in missing:
    print("  MISSING:", m)
print("\n=== where missing live in Anaconda ===")
for m in missing:
    found = []
    for base in (ANACONDA, os.path.join(ANACONDA, "Library", "bin"), os.path.join(ANACONDA, "DLLs")):
        cand = os.path.join(base, m)
        if os.path.isfile(cand):
            found.append(cand)
    print(" ", m, "->", found if found else "NOT FOUND in Anaconda")
