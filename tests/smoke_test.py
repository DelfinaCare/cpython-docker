"""Smoke tests for the runtime image.

Checks that the whole stdlib imports (minus the parts we deliberately leave
out), that every C library is the version we pinned and was loaded from
/usr/local/lib, and that each library actually works.

Expected versions come from the build args, passed in as environment
variables by the `test` stage of the Dockerfile.
"""

import importlib
import os
import subprocess
import sys
import tempfile

env = os.environ
failures = []


def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {name}{': ' + str(detail) if detail else ''}")
    if not cond:
        failures.append(name)


# --- Interpreter -------------------------------------------------------------
check("python version", sys.version.split()[0] == env["PYTHON_VERSION"], sys.version)

# --- Every stdlib module imports ---------------------------------------------
NOT_ON_LINUX = {
    "msvcrt", "nt", "winreg", "winsound", "_winapi", "_wmi", "_overlapped",
    "_scproxy", "_msi", "msilib", "_winreg",
}
LEFT_OUT = {"tkinter", "_tkinter", "idlelib", "turtle", "turtledemo", "_ios_support"}
SIDE_EFFECTS = {"antigravity", "this"}  # open a browser / print on import

import_failures = {}
for name in sorted(sys.stdlib_module_names - NOT_ON_LINUX - LEFT_OUT - SIDE_EFFECTS):
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as e:
        # Test-only extension modules are not built (--disable-test-modules).
        if not name.startswith(("_test", "_ctypes_test", "_xxtest")) and name not in (
            "xxlimited", "xxlimited_35", "xxsubtype", "_xxtestfuzz", "test",
        ):
            import_failures[name] = repr(e)
    except Exception as e:  # noqa: BLE001
        import_failures[name] = repr(e)
check("all stdlib modules import", not import_failures, import_failures or "")

for name in LEFT_OUT:
    try:
        importlib.import_module(name)
        check(f"{name} is absent", False)
    except ImportError:
        pass

# --- Linked library versions -------------------------------------------------
import bz2  # noqa: E402,F401
import ctypes  # noqa: E402
import ctypes.util  # noqa: E402
import curses  # noqa: E402
import dbm.gnu  # noqa: E402
import decimal  # noqa: E402
import lzma  # noqa: E402,F401
import pyexpat  # noqa: E402
import readline  # noqa: E402
import sqlite3  # noqa: E402
import ssl  # noqa: E402
import uuid  # noqa: E402
import zlib  # noqa: E402
from compression import zstd  # noqa: E402

v = env["OPENSSL_VERSION"]
check("openssl version", ssl.OPENSSL_VERSION.startswith(f"OpenSSL {v} "), ssl.OPENSSL_VERSION)

check("zlib version", zlib.ZLIB_RUNTIME_VERSION == env["ZLIB_VERSION"], zlib.ZLIB_RUNTIME_VERSION)

enc = int(env["SQLITE_VERSION"])  # e.g. 3540100 -> 3.54.1
sqlite_expected = f"{enc // 1000000}.{enc // 10000 % 100}.{enc // 100 % 100}"
check("sqlite version", sqlite3.sqlite_version == sqlite_expected, sqlite3.sqlite_version)

check("expat version", pyexpat.EXPAT_VERSION == f"expat_{env['EXPAT_VERSION']}", pyexpat.EXPAT_VERSION)

check("mpdecimal version", decimal.__libmpdec_version__ == env["MPDECIMAL_VERSION"],
      decimal.__libmpdec_version__)

check("zstd version", zstd.zstd_version == env["ZSTD_VERSION"], zstd.zstd_version)

check("readline version", readline._READLINE_LIBRARY_VERSION == env["READLINE_VERSION"],
      readline._READLINE_LIBRARY_VERSION)

nc = "{}.{}".format(*curses.ncurses_version[:2])
check("ncurses version", nc == env["NCURSES_VERSION"], curses.ncurses_version)

import _gdbm  # noqa: E402
gv = ".".join(map(str, _gdbm._GDBM_VERSION))
check("gdbm version", gv.startswith(env["GDBM_VERSION"]), gv)

lzma_lib = ctypes.CDLL("/usr/local/lib/liblzma.so.5")
lzma_lib.lzma_version_string.restype = ctypes.c_char_p
lv = lzma_lib.lzma_version_string().decode()
check("xz version", lv == env["XZ_VERSION"], lv)

bz_lib = ctypes.CDLL("/usr/local/lib/libbz2.so.1.0")
bz_lib.BZ2_bzlibVersion.restype = ctypes.c_char_p
bv = bz_lib.BZ2_bzlibVersion().decode()
check("bzip2 version", bv.startswith(env["BZIP2_VERSION"] + ","), bv)

# --- Libraries were loaded from /usr/local/lib, not the base image ------------
with open("/proc/self/maps") as f:
    mapped = {line.split()[-1] for line in f if "/lib" in line and ".so" in line}
for soname in ("libssl.so", "libcrypto.so", "libz.so", "libbz2.so", "liblzma.so",
               "libzstd.so", "libffi.so", "libsqlite3.so", "libexpat.so",
               "libmpdec.so", "libncursesw.so", "libpanelw.so", "libreadline.so",
               "libgdbm.so", "libgdbm_compat.so", "libuuid.so", "libpython3"):
    paths = sorted(p for p in mapped if os.path.basename(p).startswith(soname))
    check(f"{soname} loaded from /usr/local/lib",
          paths and all(p.startswith("/usr/local/lib/") for p in paths), paths)

check("ctypes.util.find_library", ctypes.util.find_library("ssl") == "libssl.so.3",
      ctypes.util.find_library("ssl"))
check("ctypes.util.find_library (gcc runtime)",
      ctypes.util.find_library("stdc++") == "libstdc++.so.6"
      and ctypes.util.find_library("gcc_s") == "libgcc_s.so.1",
      (ctypes.util.find_library("stdc++"), ctypes.util.find_library("gcc_s")))

# The manylinux system libraries a C/C++ wheel may link against.
for soname in ("libssl.so.3", "libz.so.1", "libsqlite3.so.0",
               "libgcc_s.so.1", "libstdc++.so.6"):
    try:
        ctypes.CDLL(soname)
        check(f"ctypes.CDLL({soname!r})", True)
    except OSError as e:
        check(f"ctypes.CDLL({soname!r})", False, e)

# --- Functional checks -------------------------------------------------------
ctx = ssl.create_default_context()
stats = ctx.cert_store_stats()
check("ssl default CA bundle loaded", stats["x509_ca"] > 100, stats)
check("tls 1.3 available", ssl.HAS_TLSv1_3)

import hashlib  # noqa: E402
check("hashlib openssl", "sha3_256" in hashlib.algorithms_available
      and hashlib.sha256(b"x").hexdigest().startswith("2d711642"))

data = b"python from source " * 1000
for mod in ("zlib", "gzip", "bz2", "lzma", "compression.zstd"):
    m = importlib.import_module(mod)
    check(f"{mod} roundtrip", m.decompress(m.compress(data)) == data)

# zlib is built with --solo; the VEX statements in vex/ depend on the gz* API
# really being absent from libz.so.1.
libz = ctypes.CDLL("/usr/local/lib/libz.so.1")
check("zlib built --solo (no gz* API)",
      not any(hasattr(libz, f) for f in ("gzopen", "gzwrite", "gzread", "compress", "uncompress")))
c = zlib.compressobj()
head = c.compress(data)
check("zlib compressobj.copy (HAVE_ZLIB_COPY)", zlib.decompress(head + c.copy().flush()) == data)

con = sqlite3.connect(":memory:")
con.execute("create virtual table t using fts5(body)")
con.execute("insert into t values ('hello from sqlite')")
check("sqlite fts5", con.execute("select count(*) from t where t match 'sqlite'").fetchone()[0] == 1)
con.execute("select * from json_each('[1,2,3]')")
con.enable_load_extension(True)

libc = ctypes.CDLL(None)
check("ctypes/libffi call", libc.abs(-7) == 7)

check("decimal", str(decimal.Decimal(1) / decimal.Decimal(7))[:8] == "0.142857")

import xml.etree.ElementTree as ET  # noqa: E402
check("expat parse", ET.fromstring("<a><b>1</b></a>").find("b").text == "1")

with tempfile.TemporaryDirectory() as d:
    import dbm.ndbm  # noqa: E402
    import dbm.sqlite3  # noqa: E402
    for m in (dbm.gnu, dbm.ndbm, dbm.sqlite3):
        with m.open(os.path.join(d, m.__name__), "c") as db:
            db[b"k"] = b"v"
            check(f"{m.__name__}", db[b"k"] == b"v")

import _uuid  # noqa: E402
check("uuid (libuuid)", uuid.uuid1().version == 1 and len(_uuid.generate_time_safe()[0]) == 16)

# curses needs a terminal; run setupterm in a child against /dev/null.
r = subprocess.run(
    [sys.executable, "-c",
     "import curses; curses.setupterm('xterm-256color', 1); print(curses.tigetnum('colors'))"],
    capture_output=True, text=True,
)
check("curses terminfo (xterm-256color)", r.stdout.strip() == "256", r.stdout + r.stderr)

# Paths where glibc loads libgcc_s (thread teardown at exit, C stack
# unwinding) shouldn't crash.
r = subprocess.run(
    [sys.executable, "-c",
     "import threading, time\n"
     "def spin():\n    while True: time.sleep(0.001)\n"
     "for _ in range(8): threading.Thread(target=spin, daemon=True).start()\n"
     "time.sleep(0.05)"],
    capture_output=True, text=True, timeout=60,
)
check("daemon threads at interpreter exit", r.returncode == 0, r.returncode or "")

r = subprocess.run(
    [sys.executable, "-c",
     "import faulthandler, sys\n"
     "faulthandler.dump_traceback(sys.stderr)\n"
     "getattr(faulthandler, 'dump_c_stack', lambda f: None)(sys.stderr)"],
    capture_output=True, text=True, timeout=60,
)
check("faulthandler (incl. C stack)", r.returncode == 0, r.returncode or "")

r = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, text=True)
check("pip version", r.stdout.startswith(f"pip {env['PIP_VERSION']} "), r.stdout.strip())

r = subprocess.run(["python", "-c", "print('hi')"], capture_output=True, text=True)
check("`python` alias", r.stdout == "hi\n")

print()
if failures:
    print(f"{len(failures)} check(s) failed: {failures}")
    sys.exit(1)
print("all smoke tests passed")
