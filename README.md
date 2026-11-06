# CPython from source

A multi-stage Docker build that compiles CPython **and every C library it
links against** from source. Compilation happens on `debian:trixie-slim`;
the result ships on `gcr.io/distroless/base-nossl-debian13`, which has glibc
and a few files in `/etc`, but no shell, no package manager and no OpenSSL.
Both bases are digest-pinned. It supports CPython 3.14 and newer, on
`linux/amd64` and `linux/arm64`.

Tcl/Tk (`tkinter`, IDLE, `turtle`), `_ios_support` and the CPython test
suite are left out.
Every other stdlib extension module is built; the build fails if one of them
can't be.

## Build

```bash
# Single image, native platform, default CPython (3.14)
docker buildx build -t python-from-source:3.14 .

# All CPython versions in docker-bake.hcl, amd64 + arm64
IMAGE=registry.example.com/python docker buildx bake --push

# Smoke tests (native platform, every CPython version)
docker buildx bake test

# Fast iteration: skip PGO + LTO (several times faster to build, slower interpreter)
docker buildx build --build-arg PYTHON_OPTIMIZE=false .
```

Building for the other architecture runs under QEMU emulation
(`docker run --privileged --rm tonistiigi/binfmt --install arm64`). That
works, but a PGO build of CPython under emulation is very slow. For CI,
build each platform on a native runner and merge the manifests, or use a
multi-node buildx builder (`docker buildx create --append` with an arm64
node).

## What's built

| Library   | Build arg(s)                 | Provides                         |
|-----------|------------------------------|----------------------------------|
| OpenSSL   | `OPENSSL_*`                  | `ssl`, `hashlib`                 |
| zlib      | `ZLIB_*`                     | `zlib`, `gzip`, `zipfile`        |
| bzip2     | `BZIP2_*`                    | `bz2`                            |
| xz        | `XZ_*`                       | `lzma`                           |
| zstd      | `ZSTD_*`                     | `compression.zstd`               |
| libffi    | `LIBFFI_*`                   | `ctypes`                         |
| SQLite    | `SQLITE_*`                   | `sqlite3`, `dbm.sqlite3`         |
| expat     | `EXPAT_*`                    | `xml.parsers.expat`, `xml.etree` |
| mpdecimal | `MPDECIMAL_*`                | `decimal` (C accelerator)        |
| ncurses   | `NCURSES_*`                  | `curses`, `curses.panel`         |
| readline  | `READLINE_*`                 | `readline` (REPL line editing)   |
| gdbm      | `GDBM_*`                     | `dbm.gnu`, `dbm.ndbm`            |
| libuuid   | `UTIL_LINUX_*`               | `uuid` (C accelerator)           |
| pip       | `PIP_*`                      | installed from the upstream wheel|
| CA certs  | `CACERT_*`                   | Mozilla bundle from curl.se      |
| Runtime base | `RUNTIME_IMAGE` (digest-pinned) | glibc (distroless)        |
| Build base   | `BUILD_IMAGE` (digest-pinned)   | compiler toolchain; also the source of `libgcc_s`, `libstdc++` and `ldconfig` (see below) |

`PYTHON_VERSION` / `PYTHON_SHA256` select the CPython release.

Notes:

* OpenSSL is the 3.5 LTS line (supported until 2030).
* zlib is built with `--solo`, which leaves out its `gz*` file API,
  `compress()`/`uncompress()` and its default allocator. CPython uses none
  of them: it only uses the stream API, with its own allocator, and its
  `gzip` module is pure Python. Less code shipped means fewer CVEs that
  apply; see [VEX](#vex-cves-that-dont-apply). The catch: anything else
  that links this `libz.so.1` and expects the full API would fail to load.
  Wheels are unaffected, since manylinux wheels bundle their own zlib.
* Only the shared libraries go into the final image. No headers, static
  libraries or CLI tools from the dependencies are included. CPython's own
  headers and `python3-config` are kept so a C compiler is the only thing
  needed to build extensions with pip.
* Everything is linked with an rpath of `/usr/local/lib`, so library
  loading never depends on the linker cache. `tests/check_libs.py` enforces
  this.
* Three things are copied unchanged from the Debian build image, because
  distroless `base-nossl` doesn't have them:
  * `libgcc_s.so.1` and `libstdc++.so.6`. The manylinux policy treats both
    as system libraries that wheels link against but must not bundle, so
    C/C++ wheels from PyPI (numpy, pyarrow, ...) won't import without them.
  * `ldconfig` (statically linked), with Debian's `/etc/ld.so.conf`, which
    lists `/usr/local/lib`. The last step of the runtime stage,
    `RUN ["/sbin/ldconfig"]`, builds `/etc/ld.so.cache`.
    `ctypes.util.find_library()` runs `/sbin/ldconfig -p` directly, not
    through a shell, so this is all it needs.

  These have no version `ARG`s of their own; they're updated by bumping the
  `BUILD_IMAGE` digest.
* The CA bundle replaces the one distroless ships at
  `/etc/ssl/certs/ca-certificates.crt`, which is where the image's
  `SSL_CERT_FILE` points. That leaves one bundle in the image, versioned
  here.
* Each library is compiled with `-fstack-protector-strong`,
  `-fstack-clash-protection`, `_FORTIFY_SOURCE=2` and full RELRO.
* **linux/amd64 targets x86-64-v3** (`-march=x86-64-v3`): AVX2, BMI2, FMA
  and MOVBE, i.e. Intel Haswell (2013) / AMD Zen 1 and newer. The image
  won't run on older x86 CPUs, some low-end Atom/Celeron/Pentium parts, or
  emulators and VMs that hide AVX2; there it dies with "Illegal
  instruction". Every binary also carries the `x86-64-v3` ISA marker
  (`-Wl,-z,x86-64-v3`, shown by `readelf -n` as "x86 ISA needed"). Don't
  rely on it to fail cleanly: with AVX2 masked via `GLIBC_TUNABLES`, Debian's
  glibc 2.41 still loaded the binaries. Its use here is to prove that
  `LDFLAGS` reached every link (see Tests). CPython records the flag in
  `sysconfig`, so C extensions pip compiles in this image target v3 too.
  arm64 builds are unaffected. To change it, override both build args, e.g.
  `--build-arg X86_64_CFLAGS=-march=x86-64-v2 --build-arg X86_64_LDFLAGS=-Wl,-z,x86-64-v2`,
  or set both to `""` for the plain x86-64 baseline.

## Updating a dependency

### With the updater

```bash
pip install --require-hashes -r scripts/requirements.txt   # sigstore, to verify CPython

scripts/update-deps.py                  # update everything that's out of date
scripts/update-deps.py --check          # report only; exits 1 if anything is outdated
scripts/update-deps.py --verify-pins    # re-verify everything currently pinned (CI)
scripts/update-deps.py --refresh-keys   # re-fetch the pinned signing keys
scripts/update-deps.py --only openssl,zlib
scripts/update-deps.py --skip python-3.15
```

It needs Python 3.9+ and `git`, plus `gpg`/`gpgv` and the `sigstore`
package (in the same Python that runs the script) for signature checks. For
each dependency it finds the newest stable upstream release, downloads it,
verifies it, and rewrites the `ARG`s in the `Dockerfile` (and CPython's
entries in `docker-bake.hcl`). Then review `git diff` and run
`docker buildx bake test`.

| Dependency | Where versions come from | Signature (signer pinned) | Checksum published upstream |
|------------|--------------------------|---------------------------|-----------------------------|
| OpenSSL | GitHub tags, **same major.minor line** (3.5 LTS); newer lines are reported, not adopted | GPG, OpenSSL release key | `.sha256` |
| zlib | GitHub tags | GPG, Mark Adler | zlib.net |
| bzip2 | sourceware.org listing | GPG, Mark Wielaard | — |
| xz | GitHub tags | GPG, Lasse Collin | — |
| zstd | GitHub tags | GPG, Zstandard release key | `.sha256` |
| expat | GitHub tags | GPG, Sebastian Pipping | — |
| ncurses | ftp.gnu.org | GPG, Thomas E. Dickey | — |
| readline (and its patches) | ftp.gnu.org | GPG, Chet Ramey | — |
| gdbm | ftp.gnu.org | GPG, Sergey Poznyakoff | — |
| util-linux | kernel.org listing | GPG, Karel Zak (over the uncompressed tar) | `sha256sums.asc` |
| CPython | python.org API, **within each minor** in `docker-bake.hcl`; a new final minor is reported, not added | Sigstore, the release manager's identity | python.org API |
| SQLite | sqlite.org download page | — | SHA3-256 on the download page |
| mpdecimal | bytereef.org download page | — | download page |
| pip | PyPI | — | PyPI |
| CA bundle | curl.se | — | `.sha256` |
| libffi | GitHub tags | — | — (only the pinned sha256: trust on first download) |
| base images | registry | — | the digest itself |

Any mismatch or bad signature is an error: that dependency is left
untouched and the script exits 2.

#### How signers are pinned

`keys/trust.json` is the trust policy. For each GPG-signed project it lists
the primary-key fingerprints allowed to sign it, where the key is published,
and how the fingerprint was cross-checked. For each CPython minor it lists
the release manager's Sigstore identity and OIDC issuer, as published at
<https://www.python.org/downloads/metadata/sigstore/>. The key material
itself is checked in as `keys/<project>.asc`.

* A signature is accepted only if `gpgv`, given **only that project's key**,
  reports a good signature whose primary key is one of that project's
  pinned fingerprints. A signature from any other key fails, including
  another project's maintainer or a subkey added under a different primary
  key. So does an expired or revoked key.
* CPython's `.sigstore` bundle must verify with `sigstore verify identity`
  for the pinned identity and issuer.
* Checksums are still checked when upstream publishes them. They come from
  the same server as the download, so they catch corruption, while the
  signatures catch a compromised server.

Changing `trust.json` is a trust decision and belongs in a reviewed PR:
cross-check a new fingerprint against at least two independent sources (the
maintainer's site, the project's repository, release announcements, or
Debian's `debian/upstream/signing-key.asc`) and record them in `checked`.
That's what makes a change of signer visible. The xz backdoor in 2024
shipped in releases signed by a newly added maintainer, and a pin on the
previous signer would have stopped the update for review.

`--refresh-keys` re-fetches every key from its `key_source` and rewrites
`keys/<project>.asc` (keeping only the pinned primary keys), printing what
changed: new subkeys, new expiry dates, revocations, user IDs. It never
changes the fingerprints; it fails if a source no longer contains a pinned
key. It also compares the CPython identities in `trust.json` with
python.org's table. Run it when a key is about to expire or a signature
check fails because of expiry. The expat key currently expires 2026-12-28
and the xz key 2027-02-10.

#### In CI

* `--verify-pins` re-downloads everything currently pinned. It checks the
  bytes against the pinned sha256 and every signature and checksum above,
  plus the readline patches' signatures and that the base-image digests
  still resolve. It exits 1 on any failure, **including a signature it
  couldn't check** because `gpg`/`gpgv` or `sigstore` is missing. Run it on
  every PR that touches the `Dockerfile`, `docker-bake.hcl`, `patches/` or
  `keys/`: it catches a version or sha256 edited by hand without the
  updater.
* `--check` exits 1 when something is out of date. Run it on a schedule to
  open an issue or PR.
* Docker Hub rate-limits anonymous registry requests per IP. On shared CI
  runners, set `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a read-only
  access token).

#### Also kept in sync

* **readline patches.** GNU ships readline fixes as numbered patches
  (`readline83-001`, ...), not as new tarballs. The updater mirrors the
  patches for the pinned version into `patches/readline/`, checking each
  one's signature against the readline key, and removes those for other
  versions; the readline stage applies them in order. 8.3 has six so far,
  fixing crashes in redisplay and signal handling.

Limits: util-linux's first release of a series (`2.43`, without a third
number) is skipped with a note, because the Dockerfile derives the download
directory from an `x.y.z` version. ncurses only publishes full releases; its
weekly development patches aren't tracked. libffi publishes no signature or
checksum.

### By hand

Each dependency is a pair of `ARG`s at the top of the `Dockerfile`:

```dockerfile
ARG OPENSSL_VERSION=3.5.9
ARG OPENSSL_SHA256=603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a
```

Change both values and rebuild. Downloads use `ADD --checksum`, so a wrong
hash fails the build. To compute the hash:

```bash
curl -fsSL <url> | sha256sum
```

Where upstream publishes a checksum, compare against it (OpenSSL and zstd
publish `.sha256` files; SQLite lists SHA3-256 on its download page). You
can also override a version for a one-off build without editing the file:

```bash
docker buildx build \
  --build-arg OPENSSL_VERSION=3.5.10 \
  --build-arg OPENSSL_SHA256=<sha256> .
```

Rules of thumb for each library:

* **SQLite**: `SQLITE_VERSION` is the encoded form (3.54.1 → `3540100`).
  `SQLITE_YEAR` is the directory on sqlite.org it was released under.
* **expat**: the GitHub tag (`R_2_9_0`) is derived from `EXPAT_VERSION`.
* **util-linux**: use the full `x.y.z` version; the `vx.y` directory is
  derived from it.
* **CPython**: final releases only. Also bump the matching entry in
  `docker-bake.hcl`. To add a new minor version, add an entry there.
* **Adding a new library**: call `package-metadata` at the end of its
  stage with its NVD CPE (look it up at
  <https://nvd.nist.gov/products/cpe/search>), and add it to `EXPECTED` in
  `tests/check_metadata.py`.
* **Base images**: `docker buildx imagetools inspect <image>` prints the
  current index digest. Keep `BUILD_IMAGE` and `RUNTIME_IMAGE` on the same
  Debian release: the binaries are compiled against the build image's glibc,
  so the runtime image's glibc can't be older. (`base-nossl` without a
  suffix currently points at `base-nossl-debian13`; the explicit tag keeps
  it from silently moving to a new release.)

## CI/CD (GitHub Actions)

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| `.github/workflows/test.yml` | pull requests, pushes to `main` | `update-deps.py --verify-pins`, then builds and tests every CPython version on every platform, with aggressive caching |
| `.github/workflows/release.yml` | pushing a `v*` tag | builds from scratch on native runners, pushes multi-arch images to GHCR, publishes a GitHub release with SBOMs |
| `.github/workflows/update-deps.yml` | daily, or by hand | runs the updater and opens a PR; refreshes the pinned signing keys in a separate PR |
| `.github/dependabot.yml` | weekly | updates the actions used by the workflows |

Every action is pinned to a full commit SHA, with its version in a comment;
Dependabot updates both. The CPython versions and platforms come from the
`python` group in `docker-bake.hcl` (`.github/scripts/plan.sh`), so adding a
version there adds it to CI and releases.

### Releasing

```bash
git tag v2026.10.29 && git push origin v2026.10.29
```

For each CPython version in `docker-bake.hcl`, and each platform, a job on a
native runner (`ubuntu-24.04` for amd64, `ubuntu-24.04-arm` for arm64; no
QEMU) does the following:

1. Builds the runtime image with `--no-cache --pull` and no cache import, and
   pushes it **by digest only**. It carries BuildKit provenance (`mode=max`)
   and SBOM attestations.
2. Runs the `test` stage on top of it. That reuses the layers step 1 just
   built, in the same job, and nothing older, so it tests exactly what was
   pushed.
3. Generates SPDX and CycloneDX SBOMs of the pushed image with Syft
   (`+sbom-cataloger`, so the per-library SBOMs embedded in the image are
   included).

Once every platform of a version has passed, `merge` combines the
per-platform images into one multi-arch image and tags it:

| Tag | Meaning |
|-----|---------|
| `ghcr.io/<owner>/<repo>:3.14.10-2026.10.29` | this release; never reused, so safe to pin |
| `ghcr.io/<owner>/<repo>:3.14.10` | latest release of 3.14.10 (moves) |
| `ghcr.io/<owner>/<repo>:3.14` | latest release of 3.14 (moves) |

Finally, `release` collects every job's SBOMs, the `vex/` statements and a
`SHA256SUMS` file. It creates the GitHub release **as a draft**, uploads
everything, and only then publishes. That suits immutable releases, where a
published release can't gain or change assets. The release notes list the
image digests and every pinned version. If the workflow fails before
publishing, re-running it replaces the leftover draft; a published release
is never modified.

### Repository settings

* **Immutable releases**: Settings → General → Releases. Tags of published
  releases can then never move, and their assets never change.
* **Arm64 runners**: `ubuntu-24.04-arm` is free for public repositories.
  Private repositories need arm64 larger runners (check GitHub's current
  runner docs); change `runner` in `.github/scripts/plan.sh` to match.
* **CPU baseline**: the amd64 images target x86-64-v3, and the build runs
  CPython's test suite for PGO, so x86 runners need AVX2. GitHub-hosted x86
  runners have it; self-hosted ones must too.
* **GHCR**: the first release creates the package as private. Make it
  public under the package's settings if you want it to be. The
  `org.opencontainers.image.source` label links it to this repository.
  Pushing uses `GITHUB_TOKEN` (`packages: write`); nothing to configure.
* **Updater PRs**: pull requests opened with `GITHUB_TOKEN` don't trigger the
  Test workflow. Create a GitHub App with contents and pull-requests write
  access, install it on the repository, and set the variable
  `UPDATER_APP_CLIENT_ID` and the secret `UPDATER_APP_PRIVATE_KEY`. Without
  them, enable "Allow GitHub Actions to create and approve pull requests"
  (Settings → Actions → General), and run the tests on those PRs by hand.
* **Docker Hub** (optional): set the secrets `DOCKERHUB_USERNAME` and
  `DOCKERHUB_TOKEN` (a read-only token) so the updater and `--verify-pins`
  don't hit Docker Hub's anonymous rate limit when checking the base image.
* **Branch protection**: require the Test workflow's checks on `main`.

### PR caching

The Test workflow caches in the GitHub Actions cache, with one scope per
CPython version and platform (`mode=max`, so every intermediate stage is
cached). Each job also reads the other versions' scopes for its platform,
because the dependency stages are shared. Pushes to `main` run it too, which
keeps PRs warm, since a PR can read its base branch's caches. With a warm
cache, a PR that touches nothing in the build is mostly cache hits. A
dependency bump rebuilds that library and CPython (with PGO), as a local
build would. The `ccache` and apt cache mounts aren't persisted between
runs. Repositories get 10 GB of Actions cache by default, and old entries
are evicted beyond that.

### The updater's Python dependencies

`scripts/requirements.txt` is a hash-pinned lock (all 29 packages) of
`scripts/requirements.in`, which CI installs with `--require-hashes`. To
update it, edit `requirements.in` and regenerate with Python 3.13 (the
version CI uses):

```bash
pip install pip-tools
cd scripts && pip-compile --generate-hashes --allow-unsafe --strip-extras \
  --no-emit-index-url --output-file requirements.txt requirements.in
```

## Caching

* Every dependency is its own stage, built from a shared `builder` stage.
  BuildKit builds them in parallel. A version bump invalidates only that
  stage, any stage that links against it (only `readline` depends on
  `ncurses`), and the CPython stage.
* Downloads are content-addressed (`ADD --checksum`), so they're reused
  across builds, stages and branches.
* The CPython source is unpacked before the dependencies are copied in, so a
  dependency bump doesn't redo that step either.
* In the final image, each library is a separate `COPY --link` layer. A
  bump replaces only that library's layer and the CPython layer; registries
  and clients reuse the rest. `--link` layers don't depend on the base, so a
  base-image digest bump doesn't force them to rebuild.
* pip is installed from the upstream wheel in its own stage, and lands in
  its own final layer, so a pip bump doesn't rebuild CPython.
* The runtime stage is the base plus one `COPY --link` layer per
  component. Its only `RUN` is the exec-form `ldconfig` at the end, since
  distroless has no shell. That few-KB `/etc/ld.so.cache` layer is rebuilt
  whenever anything changes.
* Compilation uses a `ccache` cache mount, which speeds up rebuilds when a
  stage is invalidated but most of its sources haven't changed (for
  example, CPython after a dependency bump).
* In CI, persist the BuildKit cache between runs, e.g.
  `--cache-to type=registry,ref=<repo>:buildcache,mode=max` and
  `--cache-from type=registry,ref=<repo>:buildcache`. `mode=max` matters:
  it exports the intermediate dependency stages too. Cache mounts (ccache,
  apt) are not exported; they only help on persistent builders.

## Build args for CPython and the CPU baseline

| Arg                      | Default | Meaning                                  |
|--------------------------|---------|------------------------------------------|
| `PYTHON_OPTIMIZE`        | `true`  | `--enable-optimizations --with-lto`      |
| `PYTHON_CONFIGURE_EXTRA` | empty   | extra `./configure` flags, e.g. `--disable-gil` |
| `X86_64_CFLAGS`          | `-march=x86-64-v3` | extra compiler flags for linux/amd64 (all libraries and CPython) |
| `X86_64_LDFLAGS`         | `-Wl,-z,x86-64-v3` | extra linker flags for linux/amd64; marks the required ISA level |

## Tests

The `test` stage runs on top of the runtime image:

* `tests/check_libs.py` runs the dynamic loader's `--list` mode (what
  `ldd` wraps) on every ELF file under `/usr/local`, plus `libgcc_s` and
  `libstdc++`. It fails if any
  dependency is missing, or if a library we built resolves from outside
  `/usr/local/lib`.
* `tests/smoke_test.py`:
  * imports every module in `sys.stdlib_module_names`, except Windows/macOS
    modules, and checks that the parts left out on purpose are absent;
  * checks that each library's runtime-reported version matches its pinned
    build arg, and that it was mapped from `/usr/local/lib`;
  * exercises each library: a TLS context loads the CA bundle, compression
    round-trips, SQLite FTS5, ctypes calls, the three dbm backends, uuid1,
    curses terminfo, and pip;
  * checks that `ctypes.util.find_library()` works and that `libgcc_s` and
    `libstdc++` load, and exercises the paths where glibc uses `libgcc_s`:
    daemon threads at interpreter exit and `faulthandler` C-stack dumps.

* `tests/check_metadata.py` checks that every ELF file under `/usr/local`
  carries a package note at the pinned version, that there's one SBOM file
  per component with matching versions, and that the copied Debian files
  have dpkg records (see "Scanning and SBOMs"). On x86_64 it also checks
  that every one of those files carries the ISA marker from
  `X86_64_LDFLAGS`, which catches any build system that ignores `LDFLAGS`.

The tests use `RUN ["python3", ...]` exec form, since there's no shell.

## Scanning and SBOMs

Without help, scanners only see the distroless Debian packages and pip.
CPython and the libraries built here aren't Debian packages, so the image
records them three ways:

| Metadata | Where | Read by |
|----------|-------|---------|
| ELF package notes ([systemd spec](https://systemd.io/ELF_PACKAGE_METADATA/)): name, version, license, NVD CPE | a `.note.package` section in every `.so`/executable | Syft and Grype, **by default** |
| One CycloneDX SBOM per component: purl, exact NVD CPE, license, source URL and sha256 | `/usr/local/share/sbom/<name>.cdx.json` | Syft with `--select-catalogers +sbom-cataloger` |
| dpkg records for `libgcc-s1`, `libstdc++6`, `libc-bin` (they really are Debian packages) | `/var/lib/dpkg/status.d/` | Trivy, Syft, Grype, by default |

Every stage records its own metadata through the `package-metadata` helper
in the `builder` stage, so it lives in that component's layer and changes
only when that component does.

What each tool gets:

* **Grype, most accurate**: generate the SBOM with Syft's SBOM reader on,
  then scan it:

  ```bash
  syft scan <image> --select-catalogers +sbom-cataloger -o syft-json > sbom.json
  grype sbom:sbom.json --vex vex/zlib.openvex.json --vex vex/util-linux.openvex.json
  ```

  Every library is matched on its exact NVD CPE. Each source-built library
  shows up twice in that SBOM, once from its ELF note and once from its
  SBOM file; that's expected. The SBOM reader also picks up the SBOMs pip
  ships for its vendored packages.
* **`grype <image>` / `syft <image>` with defaults**: every component is
  listed, from the ELF notes. Syft builds CPEs from the package name and
  ignores the note's `appCpe`, so vulnerability matching only works where
  the name equals NVD's vendor and product: openssl, zlib, sqlite, python
  and util-linux. xz (`tukaani:xz`), expat (`libexpat_project:libexpat`),
  zstd (`facebook:zstandard`), bzip2, libffi, ncurses and readline need the
  SBOM route above to be matched.
* **Trivy** only sees the dpkg records. It reads embedded CycloneDX files,
  but drops components that aren't in an ecosystem it has advisories for,
  and it deliberately doesn't do NVD CPE matching. So no metadata can make
  Trivy report CVEs in libraries built from source. Use Grype for those.
* gdbm and mpdecimal aren't in NVD's CPE dictionary at all; they're listed
  but have no CPE.
* Distroless's dpkg record for `ca-certificates` stays in the image, even
  though the bundle file itself is ours. Scanners report Debian's version
  for it; `/usr/local/share/sbom/cacert.cdx.json` records the real one.

### VEX: CVEs that don't apply

Scanners match on name and version, so they can't tell when a build leaves
out the vulnerable code. `vex/` holds [OpenVEX](https://openvex.dev)
statements for those cases; pass them to Grype with `--vex` (repeat the
flag for several files). Matching findings are hidden, or shown
as "suppressed by VEX" with `--show-suppressed`.

| File | CVE | Why it doesn't apply |
|------|-----|----------------------|
| `vex/zlib.openvex.json` | CVE-2026-85091 (`gz_vacate()` heap overflow) | zlib is built `--solo`; `gzwrite.c` isn't compiled in |
| `vex/util-linux.openvex.json` | CVE-2026-3184 (`login -h` hostname canonicalization) | only libuuid is built from util-linux; `login` is neither compiled nor shipped |
| `vex/util-linux.openvex.json` | CVE-2026-53613 (restricted `mount` target race, legacy `mount(2)` path) | only libuuid is built from util-linux; neither `mount`/`umount` nor libmount is compiled or shipped, and the base image has none |

Rules for adding one:

* Only use `not_affected` when something in this repo guarantees it, and
  add a test that fails if that ever stops being true. For zlib,
  `tests/smoke_test.py` checks that `libz.so.1` exports no `gz*` symbols.
  For util-linux, the build stage fails if it compiles anything other than
  libuuid, and `tests/check_metadata.py` checks that libuuid is the only
  file it ships and that no `login`, `mount`, `umount` or libmount exists
  anywhere in the image.
* Use the component's purl without a version as the product
  (`pkg:generic/zlib`), so the statement survives version bumps. Grype
  matches it against both the ELF-note and SBOM entries. Grype didn't match
  a statement with the image as the product and zlib nested under it,
  because that depends on the image's exact name.
* Trivy also takes `--vex`, but it doesn't see the libraries built here, so
  this only matters there for statements about Debian packages.

## Running on distroless

* There's no shell, so `docker run image sh` doesn't work, and neither
  does anything that calls `subprocess` with `shell=True` or `os.system()`.
  Use the `test` stage pattern, or a `debug` build stage, to inspect it.
* manylinux wheels work. Tested by installing numpy and pyarrow, and
  raising a C++ exception through pyarrow. On plain `base-nossl`, both fail
  to import with `libstdc++.so.6: cannot open shared object file`.
  `distroless/cc-debian13` would also provide these libraries, but it
  brings Debian's `libssl3` with them, which is the second OpenSSL that
  `base-nossl` avoids.
* If you add shared libraries in a derived image outside `/usr/local/lib`
  or the Debian multiarch directory, rerun `RUN ["/sbin/ldconfig"]` so
  `find_library()` can see them.
* The image runs as root, as distroless does by default. Add
  `USER 65532` (distroless's `nonroot` user) in a derived image to drop it.

## License

Copyright 2026 Delfina.

This repository's own files (the Dockerfile, scripts, tests, workflows,
VEX statements and documentation) are licensed under the
[Apache License 2.0](LICENSE); see also [NOTICE](NOTICE).

Third-party material in the repository keeps its own license:

* `patches/readline/`: official GNU readline patches, GPL-3.0-or-later,
  copyright the Free Software Foundation.
* `keys/*.asc`: the public signing keys of each project's maintainers,
  redistributed unmodified for signature verification.

The images contain software under many licenses: GPL-3.0-or-later
(readline, gdbm), Apache-2.0 (OpenSSL), PSF-2.0 (CPython), MIT, BSD, zlib,
public domain (SQLite), MPL-2.0 (the CA bundle), and the Debian packages of
the distroless base. Each image carries them:

* `/usr/local/share/licenses/<component>/`: every source-built component's
  license text(s), plus a `SOURCE` file with the exact upstream archive it
  was built from (URL and SHA-256). `readline/patches/` holds the patches
  applied to readline.
* `/usr/local/share/licenses/README`: an overview, including the repository
  and commit the image was built from, which provide the build scripts.
* `/usr/share/doc/<package>/copyright`: the Debian packages' licenses (from
  distroless, plus `libgcc-s1`, `libstdc++6` and `libc-bin`, copied from
  the build image).

For the GPL components, those `SOURCE` files, the patches and the build
scripts at the recorded commit are the directions to the Corresponding
Source (GPLv3 section 6(d)). The archives themselves stay on their upstream
servers (ftp.gnu.org), which keep old releases. If one ever disappeared,
you'd have to provide that source yourself. GitHub's automatic "Source
code" archives on each release contain only this repository, not the
upstream sources. `tests/check_metadata.py` fails the build if a component
is missing its license text or `SOURCE` file.
