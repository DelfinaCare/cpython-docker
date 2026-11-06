"""Verify the package metadata that lets SBOM tools and scanners see inside
the image:

- every ELF file under /usr/local carries an ELF package note
  (https://systemd.io/ELF_PACKAGE_METADATA/) naming the component it came
  from, at the version pinned by the build args;
- /usr/local/share/sbom has one CycloneDX file per component, with the same
  versions;
- the files copied from the Debian build image have dpkg records in
  /var/lib/dpkg/status.d;
- every component ships its license text(s) and a SOURCE file with its exact
  source URL and SHA-256 under /usr/local/share/licenses;
- on x86_64, every ELF file under /usr/local is marked with the x86-64 ISA
  level from X86_64_LDFLAGS (e.g. -Wl,-z,x86-64-v3), and CPython recorded
  X86_64_CFLAGS for building extensions.
"""

import json
import os
import platform
import re
import struct
import sys
import sysconfig

env = os.environ
sqlite = int(env["SQLITE_VERSION"])  # 3540100 -> 3.54.1
EXPECTED = {
    "openssl": env["OPENSSL_VERSION"],
    "zlib": env["ZLIB_VERSION"],
    "bzip2": env["BZIP2_VERSION"],
    "xz": env["XZ_VERSION"],
    "zstd": env["ZSTD_VERSION"],
    "libffi": env["LIBFFI_VERSION"],
    "sqlite": f"{sqlite // 1000000}.{sqlite // 10000 % 100}.{sqlite // 100 % 100}",
    "expat": env["EXPAT_VERSION"],
    "mpdecimal": env["MPDECIMAL_VERSION"],
    "ncurses": env["NCURSES_VERSION"],
    "readline": env["READLINE_VERSION"],
    "gdbm": env["GDBM_VERSION"],
    "util-linux": env["UTIL_LINUX_VERSION"],
    "python": env["PYTHON_VERSION"],
}
NO_CPE = {"mpdecimal", "gdbm", "cacert"}  # not in the NVD CPE dictionary

failures = []


def fail(msg):
    print("FAIL", msg)
    failures.append(msg)


def notes(path, section_name):
    """Yield (type, desc bytes) for each note in an ELF section."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise ValueError("not a 64-bit little-endian ELF")
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)

    def section(i):
        name, _type, _flags, _addr, off, size = struct.unpack_from(
            "<IIQQQQ", data, shoff + i * shentsize)
        return name, off, size

    _, stroff, _ = section(shstrndx)
    for i in range(shnum):
        name, off, size = section(i)
        if data[stroff + name:data.index(b"\0", stroff + name)] != section_name:
            continue
        end = off + size
        while off < end:
            namesz, descsz, ntype = struct.unpack_from("<III", data, off)
            desc = off + 12 + (namesz + 3) // 4 * 4
            yield ntype, data[desc:desc + descsz]
            off = desc + (descsz + 3) // 4 * 4


def package_note(path):
    """Return the JSON payload of the .note.package section, or None."""
    for _ntype, desc in notes(path, b".note.package"):
        return json.loads(desc.rstrip(b"\0"))
    return None


GNU_PROPERTY_X86_ISA_1_NEEDED = 0xC0008002


def x86_isa_needed(path):
    """Return the GNU_PROPERTY_X86_ISA_1_NEEDED bitmask (bit n = level v(n+1))."""
    for ntype, desc in notes(path, b".note.gnu.property"):
        if ntype != 5:  # NT_GNU_PROPERTY_TYPE_0
            continue
        off = 0
        while off + 8 <= len(desc):
            pr_type, pr_datasz = struct.unpack_from("<II", desc, off)
            if pr_type == GNU_PROPERTY_X86_ISA_1_NEEDED:
                return struct.unpack_from("<I", desc, off + 8)[0]
            off += 8 + (pr_datasz + 7) // 8 * 8
    return 0


# x86_64 ISA level every binary must be marked with, e.g. 3 for x86-64-v3.
isa_level = None
if platform.machine() == "x86_64":
    m = re.search(r"x86-64-v(\d)", env.get("X86_64_LDFLAGS", ""))
    isa_level = int(m.group(1)) if m else None
    cflags = env.get("X86_64_CFLAGS", "").strip()
    if cflags and cflags not in sysconfig.get_config_var("CFLAGS"):
        fail(f"sysconfig CFLAGS lacks {cflags!r}: {sysconfig.get_config_var('CFLAGS')}")

# --- ELF package notes -------------------------------------------------------
seen = {}
isa_checked = 0
for root, _dirs, files in os.walk("/usr/local"):
    for name in files:
        path = os.path.join(root, name)
        if os.path.islink(path):
            continue
        with open(path, "rb") as f:
            if f.read(4) != b"\x7fELF":
                continue
        if isa_level is not None:
            isa_checked += 1
            if not x86_isa_needed(path) & (1 << (isa_level - 1)):
                fail(f"{path}: not marked as needing x86-64-v{isa_level}")
        note = package_note(path)
        if note is None:
            fail(f"{path}: no ELF package note")
            continue
        seen.setdefault(note["name"], set()).add(path)
        want = EXPECTED.get(note["name"])
        if note.get("version") != want:
            fail(f"{path}: note says {note['name']} {note.get('version')}, expected {want}")
        if note["name"] not in NO_CPE and not note.get("appCpe", "").startswith("cpe:2.3:a:"):
            fail(f"{path}: note for {note['name']} has no appCpe")
for name in EXPECTED:
    if name not in seen:
        fail(f"no ELF file carries a package note for {name}")

# vex/util-linux.openvex.json relies on util-linux contributing nothing but
# libuuid, and on no login(1), mount(8)/umount(8) or libmount being shipped
# (from util-linux or from the base image).
ul = {os.path.basename(p) for p in seen.get("util-linux", ())}
if not ul or not all(n.startswith("libuuid.so.") for n in ul):
    fail(f"util-linux files other than libuuid: {sorted(ul)}")
for d in ("/usr/local/bin", "/usr/local/sbin", "/usr/bin", "/usr/sbin", "/bin", "/sbin"):
    for prog in ("login", "mount", "umount"):
        if os.path.lexists(os.path.join(d, prog)):
            fail(f"{d}/{prog} is present")
for top in ("/usr/local/lib", "/usr/lib", "/usr/lib64"):
    for root, _dirs, files in os.walk(top):
        for name in files:
            if name.startswith("libmount.so"):
                fail(f"{os.path.join(root, name)} is present")
print(f"ELF notes: {sum(map(len, seen.values()))} files across {len(seen)} components")
if isa_level is not None:
    print(f"x86-64-v{isa_level} ISA marker: {isa_checked} files")

# --- Embedded CycloneDX SBOMs ------------------------------------------------
sbom_dir = "/usr/local/share/sbom"
docs = {}
for fn in sorted(os.listdir(sbom_dir)):
    with open(os.path.join(sbom_dir, fn)) as f:
        doc = json.load(f)
    (comp,) = doc["components"]
    docs[comp["name"]] = comp
expected_sboms = {**EXPECTED, "cacert": env["CACERT_DATE"]}
if set(docs) != set(expected_sboms):
    fail(f"SBOM files {sorted(docs)} != expected {sorted(expected_sboms)}")
for name, comp in docs.items():
    if comp["version"] != expected_sboms.get(name):
        fail(f"SBOM {name}: version {comp['version']}, expected {expected_sboms.get(name)}")
    if name not in NO_CPE and f":{comp['version']}:" not in comp.get("cpe", ""):
        fail(f"SBOM {name}: missing or wrong cpe {comp.get('cpe')}")
print(f"CycloneDX SBOMs: {len(docs)} components")

# --- dpkg records for files copied from the Debian build image ---------------
status_d = "/var/lib/dpkg/status.d"
for pkg in ("libgcc-s1", "libstdc++6", "libc-bin"):
    path = os.path.join(status_d, pkg)
    if not os.path.exists(path):
        fail(f"missing dpkg record {path}")
        continue
    with open(path) as f:
        fields = dict(line.split(": ", 1) for line in f.read().splitlines()
                      if ": " in line and not line.startswith(" "))
    if fields.get("Package") != pkg or "installed" not in fields.get("Status", ""):
        fail(f"bad dpkg record {path}: {fields.get('Package')} {fields.get('Status')}")
    with open(path + ".md5sums") as f:
        for line in f:
            _md5, rel = line.split(None, 1)
            if not os.path.exists("/" + rel.strip()):
                fail(f"{pkg}.md5sums lists missing file /{rel.strip()}")
print("dpkg records: libgcc-s1, libstdc++6, libc-bin")

# --- License texts and source directions -------------------------------------
# Most of these licenses require their text to accompany the binaries, and
# the GPL ones need directions to the exact source (GPLv3 section 6d).
lic_dir = "/usr/local/share/licenses"
for name, version in expected_sboms.items():
    d = os.path.join(lic_dir, name)
    # The shortest real one (SQLite's public-domain dedication) is ~300
    # bytes; anything much smaller was truncated or extracted wrongly.
    texts = [f for f in os.listdir(d) if f != "SOURCE" and os.path.isfile(os.path.join(d, f))
             and os.path.getsize(os.path.join(d, f)) >= 200] if os.path.isdir(d) else []
    if not texts:
        fail(f"{d}: no license text of a plausible size")
    try:
        with open(os.path.join(d, "SOURCE")) as f:
            source = f.read()
    except OSError:
        fail(f"{d}/SOURCE missing")
        continue
    if not source.startswith(f"{name} {version}\n") or not re.search(
            r"^Source: https://\S+$", source, re.M) or not re.search(
            r"^SHA-256: [0-9a-f]{64}$", source, re.M):
        fail(f"{d}/SOURCE doesn't name {name} {version} with a URL and SHA-256: {source!r}")
try:
    with open(os.path.join(lic_dir, "sqlite", "PUBLIC-DOMAIN")) as f:
        if "disclaims copyright" not in f.read():
            fail("sqlite/PUBLIC-DOMAIN lacks SQLite's dedication")
except OSError:
    fail("sqlite/PUBLIC-DOMAIN missing")
patches = os.path.join(lic_dir, "readline", "patches")
shipped = sorted(os.listdir(patches)) if os.path.isdir(patches) else []
if not shipped or not all(re.fullmatch(r"readline\d+-\d{3}", p) for p in shipped):
    fail(f"{patches}: expected the applied readline patches, found {shipped}")
if not os.path.isfile(os.path.join(lic_dir, "README")):
    fail(f"{lic_dir}/README missing")
for pkg in ("libgcc-s1", "libstdc++6", "libc-bin"):
    path = f"/usr/share/doc/{pkg}/copyright"
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        fail(f"{path} missing")
print(f"licenses: {len(expected_sboms)} components with license text and SOURCE, "
      f"{len(shipped)} readline patches")

if failures:
    sys.exit(f"{len(failures)} metadata check(s) failed")
print("all metadata checks passed")
