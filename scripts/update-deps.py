#!/usr/bin/env python3
"""Update, verify and audit the pinned dependencies of the image.

    scripts/update-deps.py                 # update everything that's out of date
    scripts/update-deps.py --check         # report only; exit 1 if anything is outdated
    scripts/update-deps.py --verify-pins   # re-verify everything currently pinned (CI)
    scripts/update-deps.py --refresh-keys  # re-fetch the pinned signing keys
    scripts/update-deps.py --only openssl,zlib
    scripts/update-deps.py --skip python-3.15

Every download is checked against everything upstream publishes for it:
  * a GPG signature, accepted only from a primary key pinned for that
    project in keys/trust.json (key material in keys/<project>.asc);
  * a Sigstore signature for CPython, accepted only from the release
    manager identity pinned for that minor version in keys/trust.json;
  * a checksum, where upstream lists one.

Version policy:
  * OpenSSL stays on its current major.minor line (the 3.5 LTS); newer lines
    are reported, not adopted.
  * CPython is updated within each minor version listed in docker-bake.hcl;
    a new final minor release is reported, not added.
  * Everything else moves to the newest stable release.

Requires Python 3.9+ and git. Signature checks need gpg and gpgv, and, for
CPython, the sigstore package (pip install -r scripts/requirements.txt).
Without them, updates are reported as unverified and --verify-pins fails.
Set DOCKERHUB_USERNAME and DOCKERHUB_TOKEN to avoid Docker Hub's anonymous
rate limit when checking the base image.
"""

import argparse
import base64
import concurrent.futures
import dataclasses
import hashlib
import importlib.util
import json
import lzma
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE = os.path.join(ROOT, "Dockerfile")
BAKEFILE = os.path.join(ROOT, "docker-bake.hcl")
KEYS_DIR = os.path.join(ROOT, "keys")
TRUST_FILE = os.path.join(KEYS_DIR, "trust.json")
READLINE_PATCHES = os.path.join(ROOT, "patches", "readline")
PYTHON_SIGSTORE_PAGE = "https://www.python.org/downloads/metadata/sigstore/"


# --- HTTP and helpers --------------------------------------------------------

def http(url, method="GET", headers=None, tries=4):
    """Return (status, headers, body) for url, retrying transient failures."""
    req = urllib.request.Request(
        url, method=method, headers={"User-Agent": "update-deps", **(headers or {})})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, r.headers, r.read() if method == "GET" else b""
        except urllib.error.HTTPError as e:
            # Retry rate limits (Docker Hub answers 429) and server errors.
            if (e.code != 429 and e.code < 500) or attempt == tries - 1:
                return e.code, e.headers, b""
            retry_after = e.headers.get("Retry-After", "")
            if retry_after.isdigit():
                time.sleep(min(int(retry_after), 60))
                continue
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == tries - 1:
                raise
        time.sleep(5 * 2 ** attempt)
    raise AssertionError("unreachable")


def fetch(url):
    status, _, body = http(url)
    if status != 200:
        raise RuntimeError(f"GET {url}: HTTP {status}")
    return body


def fetch_text(url):
    return fetch(url).decode("utf-8", "replace")


def exists(url):
    return http(url, method="HEAD")[0] == 200


def vkey(version):
    return tuple(int(x) for x in re.findall(r"\d+", version))


def newest(versions):
    return max(versions, key=vkey) if versions else None


def git_tags(repo):
    out = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", f"https://github.com/{repo}"],
        check=True, capture_output=True, text=True, timeout=120).stdout
    return [line.split("refs/tags/", 1)[1] for line in out.splitlines()]


def tag_versions(repo, pattern):
    """Versions from tags fully matching pattern (its groups joined by '.')."""
    return [".".join(m.groups()) for t in git_tags(repo) if (m := re.fullmatch(pattern, t))]


def listing_versions(url, pattern):
    return sorted(set(re.findall(pattern, fetch_text(url))), key=vkey)


def newest_downloadable(versions, url_for, current):
    """Newest version above current whose download exists, else current."""
    for v in sorted(versions, key=vkey, reverse=True):
        if vkey(v) <= vkey(current):
            break
        if exists(url_for(v)):
            return v
    return current


def gh(repo, path):
    return f"https://github.com/{repo}/releases/download/{path}"


# --- Releases and dependencies -------------------------------------------------

@dataclasses.dataclass
class Release:
    """One pinned artifact and everything that can vouch for it."""
    url: str = ""                    # download ("" for base images)
    args: dict = dataclasses.field(default_factory=dict)  # ARG -> value, besides sha256
    sha256_arg: str = ""             # ARG holding its sha256
    checksums: list = dataclasses.field(default_factory=list)  # (alg, hex, source)
    gpg: tuple = ()                  # (trust project, signature url[, "xz"])
    sigstore: tuple = ()             # (bundle url, identity, issuer)
    image: str = ""                  # base images: name:tag@digest


@dataclasses.dataclass
class Dep:
    name: str
    current: str                     # pinned version, as the user sees it
    latest: object                   # () -> (version, note)
    release: object                  # (version) -> Release
    display: object                  # (version) -> str, for the report


def build_deps(args, bake_entries, trust):
    """The dependency table. Versions are the values of the *_VERSION ARGs."""
    deps = []

    def add(name, current, latest, release, display=lambda v: v):
        deps.append(Dep(name, current, latest, release, display))

    # Base images: the "version" is the digest.
    for name, arg in (("base-build-image", "BUILD_IMAGE"), ("base-runtime-image", "RUNTIME_IMAGE")):
        image, digest = args[arg].split("@")
        add(name, digest,
            lambda image=image: (registry_digest(image), ""),
            lambda d, image=image, arg=arg: Release(args={arg: f"{image}@{d}"},
                                                    image=f"{image}@{d}"),
            lambda d: d[7:19])

    def openssl_latest():
        cur = args["OPENSSL_VERSION"]
        series = ".".join(cur.split(".")[:2])
        tags = git_tags("openssl/openssl")
        line = [m.group(1) for t in tags
                if (m := re.fullmatch(rf"openssl-({re.escape(series)}\.\d+)", t))]
        v = newest_downloadable(line, lambda v: openssl_url(v), cur)
        newer = sorted({".".join(m.group(1).split(".")[:2]) for t in tags
                        if (m := re.fullmatch(r"openssl-(\d+\.\d+\.\d+)", t))
                        and vkey(m.group(1))[:2] > vkey(series)}, key=vkey)
        return v, f"staying on {series}; also released: {', '.join(newer)}" if newer else ""

    def openssl_url(v):
        return gh("openssl/openssl", f"openssl-{v}/openssl-{v}.tar.gz")

    def openssl_release(v):
        url = openssl_url(v)
        return Release(url, {"OPENSSL_VERSION": v}, "OPENSSL_SHA256",
                       [("sha256", fetch_text(url + ".sha256").split()[0], "openssl .sha256")],
                       ("openssl", url + ".asc"))
    add("openssl", args["OPENSSL_VERSION"], openssl_latest, openssl_release)

    def zlib_url(v):
        return gh("madler/zlib", f"v{v}/zlib-{v}.tar.gz")

    def zlib_release(v):
        url = zlib_url(v)
        # "... version 1.3.2, tar.gz format (1468K, SHA-256 hash <tt>...</tt>)"
        m = re.search(rf"version {re.escape(v)}, tar\.gz format \([^)]*?"
                      rf"SHA-256 hash <tt>([0-9a-f]{{64}})", fetch_text("https://zlib.net/"))
        return Release(url, {"ZLIB_VERSION": v}, "ZLIB_SHA256",
                       [("sha256", m.group(1), "zlib.net")] if m else [],
                       ("zlib", url + ".asc"))
    add("zlib", args["ZLIB_VERSION"],
        lambda: (newest_downloadable(tag_versions("madler/zlib", r"v(\d+(?:\.\d+)+)"),
                                     zlib_url, args["ZLIB_VERSION"]), ""),
        zlib_release)

    bzip2_base = "https://sourceware.org/pub/bzip2/"
    add("bzip2", args["BZIP2_VERSION"],
        lambda: (max(args["BZIP2_VERSION"],
                     newest(listing_versions(bzip2_base, r"bzip2-(\d+\.\d+\.\d+)\.tar\.gz")),
                     key=vkey), ""),
        lambda v: Release(f"{bzip2_base}bzip2-{v}.tar.gz", {"BZIP2_VERSION": v}, "BZIP2_SHA256",
                          gpg=("bzip2", f"{bzip2_base}bzip2-{v}.tar.gz.sig")))

    def github_dep(name, arg, repo, tag_pattern, tag_fmt, file_fmt, sig_ext=None, sha_file=False):
        def url_for(v):
            tag = tag_fmt.format(v=v, u=v.replace(".", "_"))
            return gh(repo, f"{tag}/{file_fmt.format(v=v)}")

        def release(v):
            url = url_for(v)
            checks = [("sha256", fetch_text(url + ".sha256").split()[0], f"{name} .sha256")] \
                if sha_file else []
            return Release(url, {f"{arg}_VERSION": v}, f"{arg}_SHA256", checks,
                           (name, url + sig_ext) if sig_ext else ())
        add(name, args[f"{arg}_VERSION"],
            lambda: (newest_downloadable(tag_versions(repo, tag_pattern), url_for,
                                         args[f"{arg}_VERSION"]), ""),
            release)

    github_dep("xz", "XZ", "tukaani-project/xz", r"v(\d+\.\d+\.\d+)", "v{v}",
               "xz-{v}.tar.xz", sig_ext=".sig")
    github_dep("zstd", "ZSTD", "facebook/zstd", r"v(\d+\.\d+\.\d+)", "v{v}",
               "zstd-{v}.tar.gz", sig_ext=".sig", sha_file=True)
    github_dep("libffi", "LIBFFI", "libffi/libffi", r"v(\d+\.\d+\.\d+)", "v{v}",
               "libffi-{v}.tar.gz")

    def sqlite_page():
        # The download page embeds a CSV line:
        # PRODUCT,3.54.1,2026/sqlite-autoconf-3540100.tar.gz,size,sha3-256
        return re.search(r"PRODUCT,[\d.]+,(\d{4})/sqlite-autoconf-(\d+)\.tar\.gz,\d+,([0-9a-f]{64})",
                         fetch_text("https://www.sqlite.org/download.html")).groups()

    def sqlite_display(enc):
        n = int(enc)
        return f"{n // 1000000}.{n // 10000 % 100}.{n // 100 % 100}"

    def sqlite_release(enc):
        year, latest_enc, sha3 = sqlite_page()
        if enc != latest_enc:  # older releases aren't on the page
            year, sha3 = args["SQLITE_YEAR"], None
        return Release(f"https://www.sqlite.org/{year}/sqlite-autoconf-{enc}.tar.gz",
                       {"SQLITE_VERSION": enc, "SQLITE_YEAR": year}, "SQLITE_SHA256",
                       [("sha3_256", sha3, "sqlite.org SHA3-256")] if sha3 else [])
    add("sqlite", args["SQLITE_VERSION"],
        lambda: (max(args["SQLITE_VERSION"], sqlite_page()[1], key=int), ""),
        sqlite_release, sqlite_display)

    github_dep("expat", "EXPAT", "libexpat/libexpat", r"R_(\d+)_(\d+)_(\d+)", "R_{u}",
               "expat-{v}.tar.xz", sig_ext=".asc")

    mpdec_page = "https://www.bytereef.org/mpdecimal/download.html"

    def mpdec_release(v):
        m = re.search(rf"mpdecimal-{re.escape(v)}\.tar\.gz</a>.*?([0-9a-f]{{64}})",
                      fetch_text(mpdec_page), re.S)
        return Release(f"https://www.bytereef.org/software/mpdecimal/releases/mpdecimal-{v}.tar.gz",
                       {"MPDECIMAL_VERSION": v}, "MPDECIMAL_SHA256",
                       [("sha256", m.group(1), "bytereef.org")] if m else [])
    add("mpdecimal", args["MPDECIMAL_VERSION"],
        lambda: (max(args["MPDECIMAL_VERSION"],
                     newest(re.findall(r"mpdecimal-(\d+\.\d+\.\d+)\.tar\.gz",
                                       fetch_text(mpdec_page))), key=vkey), ""),
        mpdec_release)

    for project in ("ncurses", "readline", "gdbm"):
        base = f"https://ftp.gnu.org/gnu/{project}/"
        arg = project.upper()
        add(project, args[f"{arg}_VERSION"],
            lambda base=base, project=project, arg=arg: (max(
                args[f"{arg}_VERSION"],
                newest(listing_versions(base, rf'{project}-(\d+\.\d+(?:\.\d+)?)\.tar\.gz"')),
                key=vkey), ""),
            lambda v, base=base, project=project, arg=arg: Release(
                f"{base}{project}-{v}.tar.gz", {f"{arg}_VERSION": v}, f"{arg}_SHA256",
                gpg=(project, f"{base}{project}-{v}.tar.gz.sig")))

    ul_base = "https://www.kernel.org/pub/linux/utils/util-linux/"

    def ul_latest():
        cur = args["UTIL_LINUX_VERSION"]
        full, short = [], []
        for s in listing_versions(ul_base, r'href="v(\d+\.\d+)/"')[-3:]:
            names = re.findall(r'util-linux-(\d+\.\d+(?:\.\d+)?)\.tar\.xz"',
                               fetch_text(f"{ul_base}v{s}/"))
            full += [n for n in names if n.count(".") == 2]
            short += [n for n in names if n.count(".") == 1]
        v = max(cur, newest(full), key=vkey)
        # The Dockerfile derives the vX.Y directory from an X.Y.Z version, so
        # the first release of a series (X.Y, no .Z) can't be pinned.
        skipped = [s for s in short if vkey(s) > vkey(v)]
        return v, f"skipped {', '.join(skipped)}: waiting for an X.Y.Z release" if skipped else ""

    def ul_release(v):
        d = f"{ul_base}v{'.'.join(v.split('.')[:2])}/"
        m = re.search(rf"^([0-9a-f]{{64}})\s+util-linux-{re.escape(v)}\.tar\.xz$",
                      fetch_text(d + "sha256sums.asc"), re.M)
        # kernel.org signs the uncompressed tarball.
        return Release(f"{d}util-linux-{v}.tar.xz", {"UTIL_LINUX_VERSION": v}, "UTIL_LINUX_SHA256",
                       [("sha256", m.group(1), "kernel.org sha256sums.asc")] if m else [],
                       ("util-linux", f"{d}util-linux-{v}.tar.sign", "xz"))
    add("util-linux", args["UTIL_LINUX_VERSION"], ul_latest, ul_release)

    def pip_release(v):
        d = json.loads(fetch(f"https://pypi.org/pypi/pip/{v}/json"))
        whl = next(u for u in d["urls"] if u["filename"] == f"pip-{v}-py3-none-any.whl")
        return Release(f"https://files.pythonhosted.org/packages/py3/p/pip/{whl['filename']}",
                       {"PIP_VERSION": v}, "PIP_SHA256",
                       [("sha256", whl["digests"]["sha256"], "PyPI")])
    add("pip", args["PIP_VERSION"],
        lambda: (max(args["PIP_VERSION"],
                     json.loads(fetch("https://pypi.org/pypi/pip/json"))["info"]["version"],
                     key=vkey), ""),
        pip_release)

    def cacert_release(date):
        url = f"https://curl.se/ca/cacert-{date}.pem"
        return Release(url, {"CACERT_DATE": date}, "CACERT_SHA256",
                       [("sha256", fetch_text(url + ".sha256").split()[0], "curl.se .sha256")])
    add("cacert", args["CACERT_DATE"],
        lambda: (max([args["CACERT_DATE"]] + re.findall(
            r"cacert-(\d{4}-\d{2}-\d{2})\.pem", fetch_text("https://curl.se/docs/caextract.html"))), ""),
        cacert_release)

    for minor, entry in bake_entries.items():
        def py_release(v, minor=minor):
            ident = trust["sigstore"]["python"].get(minor)
            if not ident:
                raise RuntimeError(f"no Sigstore identity pinned for CPython {minor} in keys/trust.json")
            url = f"https://www.python.org/ftp/python/{v}/Python-{v}.tar.xz"
            files = json.loads(fetch(
                f"https://www.python.org/api/v2/downloads/release_file/?release={python_releases()[v]}"))
            sha = next(f["sha256_sum"] for f in files if f["url"] == url)
            return Release(url, {f"python {minor}": v}, f"python {minor} sha256",
                           [("sha256", sha, "python.org API")],
                           sigstore=(url + ".sigstore", ident["identity"], ident["issuer"]))
        add(f"python-{minor}", entry["version"],
            lambda minor=minor, entry=entry: (max(
                entry["version"],
                newest([r for r in python_releases() if r.startswith(minor + ".")]) or "0",
                key=vkey), ""),
            py_release)
    return deps


_python_releases = None


def python_releases():
    """{version: python.org release id} for every final CPython release."""
    global _python_releases
    if _python_releases is None:
        rels = json.loads(fetch("https://www.python.org/api/v2/downloads/release/?is_published=true"))
        _python_releases = {
            m.group(1): r["resource_uri"].rstrip("/").rsplit("/", 1)[1]
            for r in rels if not r["pre_release"]
            and (m := re.fullmatch(r"Python (\d+\.\d+\.\d+)", r["name"]))}
    return _python_releases


# --- Base images ----------------------------------------------------------------

MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json"])


def registry_digest(image, reference=None):
    """Digest of image (name[:tag]), or of `reference` (a tag or digest) in its repo."""
    name, tag = image.rsplit(":", 1) if ":" in image.rsplit("/", 1)[-1] else (image, "latest")
    first = name.split("/", 1)[0]
    if "." in first or ":" in first:
        registry, repo = name.split("/", 1)
    else:
        registry, repo = "registry-1.docker.io", name if "/" in name else f"library/{name}"
    url = f"https://{registry}/v2/{repo}/manifests/{reference or tag}"
    status, hdrs, _ = http(url, "HEAD", {"Accept": MANIFEST_TYPES})
    if status == 401:  # bearer token, as the registry describes it
        params = dict(re.findall(r'(\w+)="([^"]*)"', hdrs.get("WWW-Authenticate", "")))
        token_url = (f"{params['realm']}?service={params.get('service', '')}"
                     f"&scope={params.get('scope', f'repository:{repo}:pull')}")
        auth = {}
        # Docker Hub rate-limits anonymous requests per IP; credentials (e.g.
        # a read-only access token in CI) raise the limit.
        if registry == "registry-1.docker.io" and os.environ.get("DOCKERHUB_TOKEN"):
            cred = f"{os.environ.get('DOCKERHUB_USERNAME', '')}:{os.environ['DOCKERHUB_TOKEN']}"
            auth = {"Authorization": "Basic " + base64.b64encode(cred.encode()).decode()}
        status, _, body = http(token_url, headers=auth)
        if status != 200:
            raise RuntimeError(f"GET {token_url}: HTTP {status}")
        token = json.loads(body)
        token = token.get("token") or token.get("access_token")
        status, hdrs, _ = http(url, "HEAD", {"Accept": MANIFEST_TYPES,
                                             "Authorization": f"Bearer {token}"})
    if status != 200 or not hdrs.get("Docker-Content-Digest"):
        raise RuntimeError(f"HEAD {url}: HTTP {status}")
    return hdrs["Docker-Content-Digest"]


# --- Signature verification --------------------------------------------------------

class Unverifiable(Exception):
    """A signature exists but the tools to check it are missing."""


def have_gpg():
    return bool(shutil.which("gpg") and shutil.which("gpgv"))


def have_sigstore():
    return importlib.util.find_spec("sigstore") is not None


class Verifier:
    def __init__(self, tmp, trust):
        self.tmp = tmp
        self.trust = trust
        self._keyrings = {}

    def keyring(self, project):
        """keys/<project>.asc, dearmored for gpgv."""
        if project not in self._keyrings:
            src = os.path.join(KEYS_DIR, f"{project}.asc")
            if not os.path.exists(src):
                raise RuntimeError(f"no pinned key {src}; run --refresh-keys")
            out = os.path.join(self.tmp, f"{project}.gpg")
            subprocess.run(["gpg", "--homedir", self.tmp, "--batch", "--yes", "--dearmor",
                            "-o", out, src], check=True, capture_output=True)
            self._keyrings[project] = out
        return self._keyrings[project]

    def gpg(self, project, data, sig_url, transform=None):
        """Check a detached signature; return a description of the signer."""
        if not have_gpg():
            raise Unverifiable("gpg/gpgv not installed")
        allowed = set(self.trust["gpg"][project]["fingerprints"])
        if transform == "xz":
            data = lzma.decompress(data)
        fd, path = tempfile.mkstemp(dir=self.tmp)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        sig = path + ".sig"
        with open(sig, "wb") as f:
            f.write(fetch(sig_url))
        r = subprocess.run(["gpgv", "--homedir", self.tmp, "--status-fd", "1",
                            "--keyring", self.keyring(project), sig, path],
                           capture_output=True)
        # User IDs in the status output aren't guaranteed to be UTF-8.
        status = r.stdout.decode("utf-8", "replace").splitlines()
        stderr = r.stderr.decode("utf-8", "replace")
        good = any(l.startswith("[GNUPG:] GOODSIG ") for l in status)
        validsig = [l.split() for l in status if l.startswith("[GNUPG:] VALIDSIG ")]
        # VALIDSIG <signing-key fpr> ... <primary-key fpr>
        primary = validsig[0][-1] if validsig else None
        if r.returncode != 0 or not good or primary not in allowed:
            problem = next((l for l in status if re.match(
                r"\[GNUPG:\] (EXPKEYSIG|REVKEYSIG|BADSIG|ERRSIG|NO_PUBKEY|KEYEXPIRED|KEYREVOKED)", l)),
                f"signer {primary} is not pinned for {project}" if primary else stderr.strip())
            raise RuntimeError(f"GPG check failed for {sig_url}: {problem}")
        return f"GPG {project} key {primary[-16:]}"

    def sigstore(self, data, bundle_url, identity, issuer):
        if not have_sigstore():
            raise Unverifiable("sigstore not installed (pip install -r scripts/requirements.txt)")
        fd, path = tempfile.mkstemp(dir=self.tmp)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        bundle = path + ".sigstore"
        with open(bundle, "wb") as f:
            f.write(fetch(bundle_url))
        r = subprocess.run([sys.executable, "-m", "sigstore", "verify", "identity",
                            "--bundle", bundle, "--cert-identity", identity,
                            "--cert-oidc-issuer", issuer, path], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"Sigstore check failed for {bundle_url} "
                               f"(expected {identity} via {issuer}): {r.stderr.strip()[-400:]}")
        return f"Sigstore {identity}"

    def release(self, rel, data):
        """Check data against everything rel lists. Returns (evidence, gaps)."""
        evidence, gaps = [], []
        for alg, want, source in rel.checksums:
            got = hashlib.new(alg, data).hexdigest()
            if got != want:
                raise RuntimeError(f"{rel.url}: {alg} {got} != {want} ({source})")
            evidence.append(f"{source} checksum")
        for kind, call in (("gpg", lambda: self.gpg(rel.gpg[0], data, *rel.gpg[1:])),
                           ("sigstore", lambda: self.sigstore(data, *rel.sigstore))):
            if getattr(rel, kind):
                try:
                    evidence.insert(0, call())
                except Unverifiable as e:
                    gaps.append(f"{kind} signature not checked: {e}")
        return evidence, gaps


# --- readline patches ------------------------------------------------------------

def readline_patch_names(version):
    tag = version.replace(".", "")
    status, _, body = http(f"https://ftp.gnu.org/gnu/readline/readline-{version}-patches/")
    return sorted(set(re.findall(rf'"(readline{tag}-\d{{3}})"', body.decode()))) if status == 200 else []


def local_patches():
    os.makedirs(READLINE_PATCHES, exist_ok=True)
    return sorted(f for f in os.listdir(READLINE_PATCHES) if re.fullmatch(r"readline\d+-\d{3}", f))


def patch_url(version, name):
    return f"https://ftp.gnu.org/gnu/readline/readline-{version}-patches/{name}"


# --- Key refresh -------------------------------------------------------------------

def key_summary(homedir, fprs):
    """Lines describing the pinned keys: primaries, subkeys, expiry, revocation."""
    r = subprocess.run(["gpg", "--homedir", homedir, "--with-colons", "--fixed-list-mode",
                        "--list-keys", *fprs], capture_output=True, text=True)
    lines, rec = [], None
    for f in (line.split(":") for line in r.stdout.splitlines()):
        if f[0] in ("pub", "sub"):
            rec = f
        elif f[0] == "fpr" and rec:
            flags = {"r": "REVOKED", "e": "expired"}.get(rec[1], "")
            exp = time.strftime("%Y-%m-%d", time.gmtime(int(rec[6]))) if rec[6] else "never"
            lines.append(f"{rec[0]} {f[9]} caps={rec[11]} expires={exp} {flags}".rstrip())
            rec = None
        elif f[0] == "uid":
            lines.append(f"uid {f[9]}")
    return lines


def refresh_keys(trust, only):
    """Re-fetch each pinned key and rewrite keys/<project>.asc if it changed."""
    if not have_gpg():
        sys.exit("--refresh-keys needs gpg")
    failures = 0
    for project, t in trust["gpg"].items():
        if only and project not in only:
            continue
        with tempfile.TemporaryDirectory() as home:
            try:
                data = fetch(t["key_source"])
                src = os.path.join(home, "src")
                with open(src, "wb") as f:
                    f.write(data)
                subprocess.run(["gpg", "--homedir", home, "--batch", "--import", src],
                               capture_output=True, check=False)
                missing = [fp for fp in t["fingerprints"]
                           if subprocess.run(["gpg", "--homedir", home, "--list-keys", fp],
                                             capture_output=True).returncode != 0]
                if missing:
                    raise RuntimeError(f"{t['key_source']} doesn't contain {', '.join(missing)}")
                new = subprocess.run(
                    ["gpg", "--homedir", home, "--armor", "--export-options", "export-minimal",
                     "--export", *t["fingerprints"]], capture_output=True, check=True).stdout
                new_summary = key_summary(home, t["fingerprints"])
            except Exception as e:
                print(f"{project:<12} ERROR: {e}")
                failures += 1
                continue
        path = os.path.join(KEYS_DIR, f"{project}.asc")
        old_summary = []
        if os.path.exists(path):
            with tempfile.TemporaryDirectory() as home:
                subprocess.run(["gpg", "--homedir", home, "--batch", "--import", path],
                               capture_output=True)
                old_summary = key_summary(home, t["fingerprints"])
        if new_summary == old_summary:
            print(f"{project:<12} unchanged")
            continue
        with open(path, "wb") as f:
            f.write(new)
        print(f"{project:<12} {'updated' if old_summary else 'added'} {os.path.relpath(path, ROOT)}")
        for line in sorted(set(old_summary) - set(new_summary)):
            print(f"{'':<12}   - {line}")
        for line in sorted(set(new_summary) - set(old_summary)):
            print(f"{'':<12}   + {line}")

    # CPython: compare the pinned Sigstore identities with python.org's table.
    try:
        published = {}
        for row in re.findall(r"<tr>(.*?)</tr>", fetch_text(PYTHON_SIGSTORE_PAGE), re.S):
            # Release | PEP | Release manager | OIDC issuer
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if len(cells) == 4 and re.fullmatch(r"3\.\d+", cells[0]):
                published[cells[0]] = {"identity": cells[2], "issuer": cells[3]}
        pinned = trust["sigstore"]["python"]
        for minor in sorted(set(pinned) | {m for m in published if vkey(m) >= (3, 14)}, key=vkey):
            if pinned.get(minor) != published.get(minor):
                print(f"python-{minor:<5} sigstore identity: pinned {pinned.get(minor)}, "
                      f"python.org lists {published.get(minor)}; review keys/trust.json")
                failures += 1
        print("python       sigstore identities checked against python.org")
    except Exception as e:
        print(f"python       ERROR checking sigstore identities: {e}")
        failures += 1
    sys.exit(1 if failures else 0)


# --- Files ---------------------------------------------------------------------------

BAKE_ENTRY = re.compile(
    r'\{ minor = "(?P<minor>[\d.]+)", version = "(?P<version>[\d.]+)",\s*'
    r'sha256 = "(?P<sha256>[0-9a-f]{64})" \}')


def read_args(text):
    return dict(re.findall(r"^ARG (\w+)=(\S*)$", text, re.M))


def set_arg(text, name, value):
    new, n = re.subn(rf"^ARG {name}=\S*$", f"ARG {name}={value}", text, flags=re.M)
    if n != 1:
        raise RuntimeError(f"expected exactly one 'ARG {name}=' line, found {n}")
    return new


# --- Main ------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="only report; exit 1 if anything is out of date")
    mode.add_argument("--verify-pins", action="store_true",
                      help="re-download and verify everything currently pinned; "
                           "exit 1 if anything fails or can't be checked")
    mode.add_argument("--refresh-keys", action="store_true",
                      help="re-fetch the signing keys pinned in keys/trust.json")
    ap.add_argument("--only", default="", help="comma-separated dependency names")
    ap.add_argument("--skip", default="", help="comma-separated dependency names")
    opts = ap.parse_args()

    with open(TRUST_FILE) as f:
        trust = json.load(f)
    only = {n for n in opts.only.split(",") if n}
    skip = {n for n in opts.skip.split(",") if n}
    if opts.refresh_keys:
        refresh_keys(trust, only)

    with open(DOCKERFILE) as f:
        dockerfile = f.read()
    with open(BAKEFILE) as f:
        bake = f.read()
    args = read_args(dockerfile)
    bake_entries = {m["minor"]: m for m in BAKE_ENTRY.finditer(bake)}
    deps = build_deps(args, bake_entries, trust)
    names = [d.name for d in deps] + ["readline-patches"]
    if unknown := (only | skip) - set(names):
        ap.error(f"unknown dependencies: {', '.join(sorted(unknown))} (known: {', '.join(names)})")
    selected = [d for d in deps if (not only or d.name in only) and d.name not in skip]
    want_patches = (not only or {"readline", "readline-patches"} & only) and \
        not {"readline", "readline-patches"} & skip

    results, errors = {}, {}
    with tempfile.TemporaryDirectory() as tmp, \
            concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        verifier = Verifier(tmp, trust)

        def download_and_verify(rel, pinned_sha=None):
            if rel.image:
                # When updating, the digest just came from the registry. When
                # verifying pins, check the pinned digest still resolves there.
                if opts.verify_pins:
                    image, digest = rel.image.split("@")
                    registry_digest(image, digest)
                return "", ["registry digest"], []
            data = fetch(rel.url)
            sha = hashlib.sha256(data).hexdigest()
            if pinned_sha and sha != pinned_sha:
                raise RuntimeError(f"{rel.url}: sha256 {sha} != pinned {pinned_sha}")
            evidence, gaps = verifier.release(rel, data)
            return sha, evidence, gaps

        def run(dep):
            if opts.verify_pins:
                rel = dep.release(dep.current)
                pinned = (bake_entries[dep.name[7:]]["sha256"] if dep.name.startswith("python-")
                          else args.get(rel.sha256_arg))
                _, evidence, gaps = download_and_verify(rel, pinned)
                return dep.current, "", rel, evidence, gaps
            version, note = dep.latest()
            if version == dep.current or opts.check:
                return version, note, None, [], []
            rel = dep.release(version)
            sha, evidence, gaps = download_and_verify(rel)
            if rel.sha256_arg:
                rel.args[rel.sha256_arg] = sha
            return version, note, rel, evidence, gaps

        futures = {pool.submit(run, d): d.name for d in selected}
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                results[name] = fut.result()
            except Exception as e:  # report it and carry on with the rest
                errors[name] = f"{type(e).__name__}: {e}"

        # readline patches follow whichever readline version ends up pinned.
        patch_line, patch_gaps = None, []
        if want_patches:
            rl = results.get("readline")
            rl_version = rl[0] if rl and not opts.verify_pins else args["READLINE_VERSION"]
            tag = rl_version.replace(".", "")
            try:
                local = local_patches()
                if opts.verify_pins:
                    mine = [p for p in local if p.startswith(f"readline{tag}-")]
                    for p in mine:
                        with open(os.path.join(READLINE_PATCHES, p), "rb") as f:
                            verifier.gpg("readline", f.read(), patch_url(rl_version, p) + ".sig")
                    stray = [p for p in local if p not in mine]
                    if stray:
                        raise RuntimeError(f"patches for another readline version: {', '.join(stray)}")
                    patch_line = f"{len(mine)} patches, GPG readline key"
                else:
                    upstream = readline_patch_names(rl_version)
                    missing = [p for p in upstream if p not in local]
                    stale = [p for p in local if not p.startswith(f"readline{tag}-")]
                    if not opts.check:
                        for p in missing:
                            data = fetch(patch_url(rl_version, p))
                            try:
                                verifier.gpg("readline", data, patch_url(rl_version, p) + ".sig")
                            except Unverifiable as e:
                                patch_gaps.append(f"{p}: {e}")
                            with open(os.path.join(READLINE_PATCHES, p), "wb") as f:
                                f.write(data)
                        for p in stale:
                            os.remove(os.path.join(READLINE_PATCHES, p))
                    if missing or stale:
                        verb = ("to add", "to remove") if opts.check else ("added", "removed")
                        patch_line = (f"{verb[0]}: {', '.join(missing) or 'none'}; "
                                      f"{verb[1]}: {', '.join(stale) or 'none'}")
                    else:
                        patch_line = "up to date"
            except Unverifiable as e:
                patch_gaps.append(str(e))
            except Exception as e:
                errors["readline-patches"] = f"{type(e).__name__}: {e}"

    # Apply updates.
    changed = [n for n, r in results.items() if r[2] is not None] if not opts.verify_pins else []
    if not opts.check and not opts.verify_pins:
        for name in changed:
            version, _, rel, _, _ = results[name]
            if name.startswith("python-"):
                minor = name[7:]
                old = bake_entries[minor]
                sha = rel.args[f"python {minor} sha256"]
                bake = bake.replace(old.group(0), old.group(0).replace(old["version"], version)
                                    .replace(old["sha256"], sha))
                if args["PYTHON_VERSION"].startswith(minor + "."):
                    dockerfile = set_arg(dockerfile, "PYTHON_VERSION", version)
                    dockerfile = set_arg(dockerfile, "PYTHON_SHA256", sha)
                continue
            for key, value in rel.args.items():
                dockerfile = set_arg(dockerfile, key, value)
        if changed:
            with open(DOCKERFILE, "w") as f:
                f.write(dockerfile)
            with open(BAKEFILE, "w") as f:
                f.write(bake)

    # Report.
    gaps_total = 0
    width = max(map(len, names))
    head = "verified by" if opts.verify_pins else "result"
    print(f"{'dependency':<{width}}  {'pinned' if opts.verify_pins else 'current':<14} "
          f"{'' if opts.verify_pins else 'latest':<14} {head}")
    for dep in selected:
        cur = dep.display(dep.current)
        if dep.name in errors:
            print(f"{dep.name:<{width}}  {cur:<14} {'':<14} ERROR: {errors[dep.name]}")
            continue
        version, note, rel, evidence, gaps = results[dep.name]
        gaps_total += len(gaps)
        if opts.verify_pins:
            status = ", ".join(evidence) or "pinned sha256 only (upstream publishes nothing to check)"
            print(f"{dep.name:<{width}}  {cur:<14} {'':<14} {status}")
        else:
            if version == dep.current:
                status = "up to date"
            elif opts.check:
                status = "update available"
            else:
                status = "updated; " + (", ".join(evidence) or
                                        "UNVERIFIED: upstream publishes no checksum or signature")
            print(f"{dep.name:<{width}}  {cur:<14} {dep.display(version):<14} {status}")
        for g in gaps:
            print(f"{'':<{width}}  UNVERIFIED: {g}")
        if note:
            print(f"{'':<{width}}  note: {note}")
    if "readline-patches" in errors:
        print(f"{'readline-patches':<{width}}  ERROR: {errors['readline-patches']}")
    elif patch_line:
        print(f"{'readline-patches':<{width}}  {patch_line}")
        if patch_line.startswith(("to add", "added")):
            changed.append("readline-patches")
    for g in patch_gaps:
        print(f"{'':<{width}}  UNVERIFIED: {g}")
    gaps_total += len(patch_gaps)

    if not opts.verify_pins and any(d.name.startswith("python-") for d in selected):
        try:
            have = max(vkey(m) for m in bake_entries)
            new = sorted({".".join(v.split(".")[:2]) for v in python_releases()
                          if vkey(v)[:2] > have}, key=vkey)
            if new:
                print(f"\nnote: CPython {', '.join(new)} has a final release; add it to "
                      f"PYTHON_RELEASES in docker-bake.hcl (and its Sigstore identity to "
                      f"keys/trust.json) to build it.")
        except Exception as e:
            print(f"\nnote: could not check for new CPython minors: {e}")

    if errors:
        print(f"\n{len(errors)} check(s) failed; see ERROR lines above.")
    if opts.verify_pins:
        if gaps_total:
            print(f"\n{gaps_total} signature(s) could not be checked; install gpg/gpgv and "
                  f"`pip install -r scripts/requirements.txt`.")
        sys.exit(1 if errors or gaps_total else 0)
    if opts.check:
        sys.exit(1 if changed or errors else 0)
    if changed:
        print("\nNext: review the diff (git diff), then run `docker buildx bake test`.\n"
              "Also check that the statements in vex/ still hold for the new versions.")
    sys.exit(2 if errors else 0)


if __name__ == "__main__":
    main()
