"""Verify that every ELF file under /usr/local (plus the GCC runtime libraries
copied from the build image) resolves all of its shared library dependencies,
and that any library we built ourselves is resolved from /usr/local/lib rather
than from a same-named copy in the base image.

The runtime image has no shell and no ldd, so this asks the dynamic loader
directly (`ld.so --list`, which is all ldd does).
"""

import glob
import os
import subprocess
import sys

loaders = sorted(glob.glob("/usr/lib*/ld-linux*.so.*") + glob.glob("/lib*/ld-linux*.so.*"))
if not loaders:
    sys.exit("check-libs: no dynamic loader found")
loader = loaders[0]

ours = {name for name in os.listdir("/usr/local/lib") if ".so." in name}


def is_elf(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def candidates():
    for root, _dirs, files in os.walk("/usr/local"):
        for name in files:
            yield os.path.join(root, name)
    yield from glob.glob("/usr/lib/*/libgcc_s.so.*")
    yield from glob.glob("/usr/lib/*/libstdc++.so.*")


failed = False
checked = 0
for path in candidates():
    if os.path.islink(path) or not is_elf(path):
        continue
    checked += 1
    r = subprocess.run([loader, "--list", path], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"loader failed on {path}:\n{r.stdout}{r.stderr}")
        failed = True
        continue
    for line in r.stdout.splitlines():
        parts = line.split()
        if "not found" in line:
            print(f"MISSING DEPENDENCY in {path}: {line.strip()}")
            failed = True
        elif len(parts) >= 3 and parts[1] == "=>" and parts[0] in ours \
                and not parts[2].startswith("/usr/local/lib/"):
            print(f"{path} resolves {parts[0]} outside /usr/local/lib: {parts[2]}")
            failed = True

if failed:
    sys.exit(1)
print(f"check-libs: {checked} ELF files, all dependencies resolved, "
      "all of ours from /usr/local/lib")
