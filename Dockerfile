# syntax=docker/dockerfile:1

# =============================================================================
# CPython built from source, with every C library it links against also built
# from source. Compiled on Debian; shipped on distroless (no shell, no package
# manager, nothing but glibc and a few config files underneath CPython).
#
# Each dependency lives in its own build stage and is pinned by version and
# sha256 below. Bumping one dependency only rebuilds that stage, the stages
# that link against it, and the final CPython stage; every other stage and
# every other layer of the runtime image stays cached.
#
# Supported: CPython 3.14 and newer, linux/amd64 and linux/arm64.
# Not included: Tcl/Tk (tkinter, IDLE, turtle), _ios_support and the CPython
# test suite.
# =============================================================================

# ---- Base images ------------------------------------------------------------
# Both pinned by multi-arch index digest. They must be the same Debian release:
# everything is compiled against the build image's glibc and runs on the
# runtime image's glibc, so the runtime's glibc can't be older.
#
# BUILD_IMAGE: toolchain only, never shipped.
ARG BUILD_IMAGE=debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
# RUNTIME_IMAGE: glibc + /etc basics, no shell, no OpenSSL. Bump the digest to
# pick up glibc security fixes.
ARG RUNTIME_IMAGE=gcr.io/distroless/base-nossl-debian13@sha256:3a2dbf10c2fdafe5865e2b03a4a3b9b23f2e280229a81730415379b76dc70592

# ---- CPU baseline -----------------------------------------------------------
# Extra compiler/linker flags for linux/amd64 only (arm64 builds use the
# defaults). x86-64-v3 = Haswell (2013) / Zen 1 and newer: AVX2, BMI2, FMA,
# MOVBE; on older CPUs the image dies with "Illegal instruction". The -z flag
# records the required level in every binary (readelf -n shows "x86 ISA
# needed"); Debian's glibc 2.41 doesn't refuse to load on that basis, so it's
# documentation, and tests/check_metadata.py uses it to prove LDFLAGS reached
# every link. Set both to "" for the plain x86-64 baseline.
ARG X86_64_CFLAGS="-march=x86-64-v3"
ARG X86_64_LDFLAGS="-Wl,-z,x86-64-v3"
# Set by BuildKit for each platform being built.
ARG TARGETARCH

# ---- CPython ----------------------------------------------------------------
# Final releases only (the download URL is /ftp/python/<version>/).
ARG PYTHON_VERSION=3.14.10
ARG PYTHON_SHA256=de414a57119abef52784d8993e341d53aed635fc2b05c2607d9f98a2c5b72032
# PGO + LTO. Builds take several times longer, but the interpreter is ~10-20%
# faster. Set to "false" for quick iteration.
ARG PYTHON_OPTIMIZE=true
# Extra ./configure flags, e.g. "--disable-gil" for a free-threaded build.
ARG PYTHON_CONFIGURE_EXTRA=""

# ---- pip (installed from the wheel, independent of the CPython build) -------
ARG PIP_VERSION=26.3
ARG PIP_SHA256=f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13

# ---- CA certificates (Mozilla bundle, as published by curl.se) --------------
ARG CACERT_DATE=2026-11-05
ARG CACERT_SHA256=427c064216761a1e72a8b30f0d954d08cb6e4435a82091fb865afd20c8497285

# ---- C library dependencies -------------------------------------------------
ARG OPENSSL_VERSION=3.5.9
ARG OPENSSL_SHA256=603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a
ARG ZLIB_VERSION=1.4
ARG ZLIB_SHA256=02dd159783677a749c04adbdd02b55b8a2bcc7e054f51b76b549199a916ad584
ARG BZIP2_VERSION=1.0.8
ARG BZIP2_SHA256=ab5a03176ee106d3f0fa90e381da478ddae405918153cca248e682cd0c4a2269
ARG XZ_VERSION=5.8.4
ARG XZ_SHA256=4ce24038fd4221e0d13bc1a2de7a4db56e90b92b3bf75321f6c14be73f65de4b
ARG ZSTD_VERSION=1.5.7
ARG ZSTD_SHA256=eb33e51f49a15e023950cd7825ca74a4a2b43db8354825ac24fc1b7ee09e6fa3
ARG LIBFFI_VERSION=3.8.0
ARG LIBFFI_SHA256=7da3e2d9a171eb0a038f592ecad3ff2bb2550f3496d87b3b29ad0cf4430c0db4
# SQLite tarballs are named by an encoded version (3.54.1 -> 3540100) and live
# under a per-year directory.
ARG SQLITE_VERSION=3540100
ARG SQLITE_YEAR=2026
ARG SQLITE_SHA256=926ef8d3eab93fbf6e06011901273eec076b25594a4e1cdadd50c987f4f6d64e
ARG EXPAT_VERSION=2.10.0
ARG EXPAT_SHA256=c624ba2998fc65df1163a49641ff3f9ab73138b11641d92c86bee0fb01b1bc7f
ARG MPDECIMAL_VERSION=4.0.1
ARG MPDECIMAL_SHA256=96d33abb4bb0070c7be0fed4246cd38416188325f820468214471938545b1ac8
ARG NCURSES_VERSION=6.6
ARG NCURSES_SHA256=355b4cbbed880b0381a04c46617b7656e362585d52e9cf84a67e2009b749ff11
ARG READLINE_VERSION=8.3
ARG READLINE_SHA256=fe5383204467828cd495ee8d1d3c037a7eba1389c22bc6a041f627976f9061cc
ARG GDBM_VERSION=1.26
ARG GDBM_SHA256=6a24504a14de4a744103dcb936be976df6fbe88ccff26065e54c1c47946f4a5e
# libuuid is built from the util-linux tarball (only the library, no programs).
# Use a full x.y.z version; the download directory is derived as vx.y.
ARG UTIL_LINUX_VERSION=2.42.4
ARG UTIL_LINUX_SHA256=fbd62a100ab7bb8746ba0661255c3c48185b1e9021507c624da01fbc696330ec


# =============================================================================
# builder: toolchain shared by every compile stage
# =============================================================================
FROM ${BUILD_IMAGE} AS builder-base

RUN --mount=type=cache,id=apt-cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=apt-lists,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      ccache \
      gcc \
      libc6-dev \
      make \
      patch \
      perl \
      pkgconf \
      xz-utils

# CFLAGS carries -O2 because autoconf projects drop their own -O when CFLAGS
# is set. Stages whose upstream default is higher (OpenSSL, zlib, zstd,
# libffi: -O3) or that pick their own level (CPython) adjust it, so every
# component builds at the optimization level its upstream chose.
#
# Everything is installed into /usr/local. Each stage installs with
# DESTDIR=/out; later stages COPY a dependency's /out into their own /.
# The rpath makes every library and extension module find our copies in
# /usr/local/lib ahead of any same-soname copies the base image ships
# (e.g. Debian's own libz.so.1 and liblzma.so.5).
ENV PATH=/usr/lib/ccache:$PATH \
    CCACHE_DIR=/root/.cache/ccache \
    PKG_CONFIG_PATH=/usr/local/lib/pkgconfig \
    CPPFLAGS="-I/usr/local/include -D_FORTIFY_SOURCE=2" \
    CFLAGS="-O2 -pipe -fstack-protector-strong -fstack-clash-protection" \
    LDFLAGS="-L/usr/local/lib -Wl,-rpath,/usr/local/lib -Wl,-z,relro,-z,now"

# Splits a stage's /out into /runtime: only what the final image needs
# (shared libraries and runtime data), stripped. No headers, static
# libraries, pkg-config files, docs or executables.
COPY --chmod=0755 <<'EOF' /usr/local/sbin/split-runtime
#!/bin/sh
set -eu
rm -rf /runtime
cp -a /out /runtime
cd /runtime/usr/local
rm -rf bin sbin include lib/pkgconfig lib/cmake share/man share/doc share/info share/aclocal
find . \( -name '*.a' -o -name '*.la' \) -delete
find . -type f -name '*.so*' -exec strip --strip-unneeded {} +
EOF

# Copies a component's license texts into
# $ROOT/usr/local/share/licenses/NAME/, which the runtime image ships (most
# of these licenses require their text to accompany binaries).
# Usage: install-licenses NAME FILE...
COPY --chmod=0755 <<'EOF' /usr/local/sbin/install-licenses
#!/bin/sh
set -eu
dir=${ROOT:-/runtime}/usr/local/share/licenses/$1
shift
mkdir -p "$dir"
for f in "$@"; do
  install -m644 "$f" "$dir/$(basename "$f")"
done
EOF

# Records what a stage built, where scanners look for it:
#  - an ELF package note (https://systemd.io/ELF_PACKAGE_METADATA/) in every
#    ELF file under $ROOT. Syft and Grype read these by default; Syft merges
#    all files with the same note into one package.
#  - a CycloneDX SBOM at $ROOT/usr/local/share/sbom/NAME.cdx.json carrying the
#    exact NVD CPE. Syft reads these with its sbom-cataloger.
#  - $ROOT/usr/local/share/licenses/NAME/SOURCE: where the exact source it was
#    built from can be downloaded, and its SHA-256. For the GPL components,
#    these are the directions to the Corresponding Source (GPLv3 section 6d).
# Usage: package-metadata NAME VERSION LICENSE CPE_VENDOR:PRODUCT|- URL SHA256
COPY --chmod=0755 <<'EOF' /usr/local/sbin/package-metadata
#!/bin/sh
set -eu
root=${ROOT:-/runtime}
name=$1 version=$2 license=$3 cpe_vp=$4 url=$5 sha256=$6
purl="pkg:generic/$name@$version"
note="{\"type\":\"generic\",\"name\":\"$name\",\"version\":\"$version\""
note="$note,\"license\":\"$license\""
cpe_json=""
if [ "$cpe_vp" != - ]; then
  cpe="cpe:2.3:a:$cpe_vp:$version:*:*:*:*:*:*:*"
  note="$note,\"appCpe\":\"$cpe\""
  cpe_json="
      \"cpe\": \"$cpe\","
fi
note="$note}"
tmp=$(mktemp -d)
# Let the linker encode the note, then graft that section onto each file.
: | gcc -x c -shared -o "$tmp/note.so" - -Xlinker --package-metadata="$note"
objcopy -O binary --only-section=.note.package "$tmp/note.so" "$tmp/note.bin"
find "$root" -type f | while read -r f; do
  [ "$(head -c4 "$f" | od -An -c | tr -d ' ')" = '177ELF' ] || continue
  objcopy --add-section .note.package="$tmp/note.bin" \
    --set-section-flags .note.package=noload,readonly "$f"
done
rm -rf "$tmp"

mkdir -p "$root/usr/local/share/licenses/$name"
cat > "$root/usr/local/share/licenses/$name/SOURCE" <<TXT
$name $version
Source: $url
SHA-256: $sha256
TXT

mkdir -p "$root/usr/local/share/sbom"
cat > "$root/usr/local/share/sbom/$name.cdx.json" <<JSON
{
  "bomFormat": "CycloneDX",
  "specVersion": "1.6",
  "version": 1,
  "components": [
    {
      "type": "library",
      "bom-ref": "$purl",
      "name": "$name",
      "version": "$version",
      "purl": "$purl",$cpe_json
      "licenses": [{"expression": "$license"}],
      "externalReferences": [
        {
          "type": "distribution",
          "url": "$url",
          "hashes": [{"alg": "SHA-256", "content": "$sha256"}]
        }
      ]
    }
  ]
}
JSON
EOF

WORKDIR /build


# Per-architecture flags on top of builder-base. BuildKit only builds the
# stage that matches the target platform.
FROM builder-base AS builder-amd64
ARG X86_64_CFLAGS
ARG X86_64_LDFLAGS
ENV CFLAGS="$CFLAGS $X86_64_CFLAGS" \
    LDFLAGS="$LDFLAGS $X86_64_LDFLAGS"

FROM builder-base AS builder-arm64

FROM builder-${TARGETARCH} AS builder


# =============================================================================
# Dependency stages. All of these run in parallel.
# =============================================================================

FROM builder AS openssl
ARG OPENSSL_VERSION
ARG OPENSSL_SHA256
ENV SRC_URL=https://github.com/openssl/openssl/releases/download/openssl-${OPENSSL_VERSION}/openssl-${OPENSSL_VERSION}.tar.gz
ADD --checksum=sha256:${OPENSSL_SHA256} ${SRC_URL} /tmp/src.tar.gz
RUN --mount=type=cache,id=ccache,target=/root/.cache/ccache <<EOF
set -eux
# Upstream builds with -O3, but setting CFLAGS replaces its default.
export CFLAGS="$(echo "$CFLAGS" | sed 's/-O2/-O3/')"
tar -xf /tmp/src.tar.gz --strip-components=1
# --openssldir=/etc/ssl: default CA paths are /etc/ssl/cert.pem and
# /etc/ssl/certs, which is where the runtime image puts the CA bundle.
./Configure --prefix=/usr/local --libdir=lib --openssldir=/etc/ssl \
    shared no-tests no-docs
make -j"$(nproc)" build_sw
make DESTDIR=/out install_sw install_ssldirs
split-runtime
# Keep only the default config; drop CA.pl/tsget helpers and .dist copies.
rm -rf /runtime/etc/ssl/misc /runtime/etc/ssl/*.dist
install-licenses openssl /build/LICENSE.txt
package-metadata openssl "$OPENSSL_VERSION" Apache-2.0 openssl:openssl "$SRC_URL" "$OPENSSL_SHA256"
EOF

FROM builder AS zlib
ARG ZLIB_VERSION
ARG ZLIB_SHA256
ENV SRC_URL=https://github.com/madler/zlib/releases/download/v${ZLIB_VERSION}/zlib-${ZLIB_VERSION}.tar.gz
ADD --checksum=sha256:${ZLIB_SHA256} ${SRC_URL} /tmp/src.tar.gz
RUN --mount=type=cache,id=ccache,target=/root/.cache/ccache <<EOF
set -eux
# Upstream builds with -O3, but setting CFLAGS replaces its default.
export CFLAGS="$(echo "$CFLAGS" | sed 's/-O2/-O3/')"
tar -xf /tmp/src.tar.gz --strip-components=1
# --solo (Z_SOLO) leaves out the gz* file API (gzread.c, gzwrite.c, ...),
# compress()/uncompress(), and zlib's default allocator. CPython uses none of
# them: it only uses the deflate/inflate stream API, with its own allocator,
# and its gzip module is pure Python. See vex/ for the CVEs this rules out.
./configure --prefix=/usr/local --shared --solo
# Build only the library: the default target also links the example and
# minigzip programs, which need the gz* API.
make -j"$(nproc)" libz.a libz.so.${ZLIB_VERSION}
make DESTDIR=/out install
split-runtime
install-licenses zlib /build/LICENSE
package-metadata zlib "$ZLIB_VERSION" Zlib zlib:zlib "$SRC_URL" "$ZLIB_SHA256"
EOF

FROM builder AS bzip2
ARG BZIP2_VERSION
ARG BZIP2_SHA256
ENV SRC_URL=https://sourceware.org/pub/bzip2/bzip2-${BZIP2_VERSION}.tar.gz
ADD --checksum=sha256:${BZIP2_SHA256} ${SRC_URL} /tmp/src.tar.gz
# bzip2 1.0.x has no configure script, no DESTDIR support and no .pc file,
# so the shared library is built and installed by hand.
RUN --mount=type=cache,id=ccache,target=/root/.cache/ccache <<EOF
set -eux
tar -xf /tmp/src.tar.gz --strip-components=1
# Its link step ignores LDFLAGS, so pass them through CC (gcc ignores linker
# flags when only compiling).
make -j"$(nproc)" -f Makefile-libbz2_so CC="gcc $LDFLAGS" \
    CFLAGS="$CFLAGS $CPPFLAGS -fPIC -D_FILE_OFFSET_BITS=64"
lib=/out/usr/local/lib
install -Dm644 bzlib.h /out/usr/local/include/bzlib.h
install -Dm755 libbz2.so.${BZIP2_VERSION} $lib/libbz2.so.${BZIP2_VERSION}
ln -s libbz2.so.${BZIP2_VERSION} $lib/libbz2.so.1.0
ln -s libbz2.so.1.0 $lib/libbz2.so
mkdir -p $lib/pkgconfig
cat > $lib/pkgconfig/bzip2.pc <<PC
prefix=/usr/local
libdir=\${prefix}/lib
includedir=\${prefix}/include
Name: bzip2
Description: A file compression library
Version: ${BZIP2_VERSION}
Libs: -L\${libdir} -lbz2
Cflags: -I\${includedir}
PC
split-runtime
install-licenses bzip2 /build/LICENSE
package-metadata bzip2 "$BZIP2_VERSION" bzip2-1.0.6 bzip:bzip2 "$SRC_URL" "$BZIP2_SHA256"
EOF

FROM builder AS xz
ARG XZ_VERSION
ARG XZ_SHA256
ENV SRC_URL=https://github.com/tukaani-project/xz/releases/download/v${XZ_VERSION}/xz-${XZ_VERSION}.tar.xz
ADD --checksum=sha256:${XZ_SHA256} ${SRC_URL} /tmp/src.tar.xz
RUN --mount=type=cache,id=ccache,target=/root/.cache/ccache <<EOF
set -eux
tar -xf /tmp/src.tar.xz --strip-components=1
./configure --prefix=/usr/local --disable-static --disable-doc --disable-nls \
    --disable-xz --disable-xzdec --disable-lzmadec --disable-lzmainfo \
    --disable-lzma-links --disable-scripts
make -j"$(nproc)"
make DESTDIR=/out install
split-runtime
# COPYING explains the per-part licensing; liblzma is 0BSD.
install-licenses xz /build/COPYING /build/COPYING.0BSD
package-metadata xz "$XZ_VERSION" 0BSD tukaani:xz "$SRC_URL" "$XZ_SHA256"
EOF

FROM builder AS zstd
ARG ZSTD_VERSION
ARG ZSTD_SHA256
ENV SRC_URL=https://github.com/facebook/zstd/releases/download/v${ZSTD_VERSION}/zstd-${ZSTD_VERSION}.tar.gz
ADD --checksum=sha256:${ZSTD_SHA256} ${SRC_URL} /tmp/src.tar.gz
RUN --mount=type=cache,id=ccache,target=/root/.cache/ccache <<EOF
set -eux
# Upstream builds with -O3, but setting CFLAGS replaces its default.
export CFLAGS="$(echo "$CFLAGS" | sed 's/-O2/-O3/')"
tar -xf /tmp/src.tar.gz --strip-components=1
make -j"$(nproc)" -C lib lib-mt PREFIX=/usr/local
make -C lib install-pc install-includes install-shared PREFIX=/usr/local DESTDIR=/out
split-runtime
# Dual-licensed: BSD-3-Clause (LICENSE) or GPL-2.0-only (COPYING).
install-licenses zstd /build/LICENSE /build/COPYING
package-metadata zstd "$ZSTD_VERSION" "BSD-3-Clause OR GPL-2.0-only" facebook:zstandard "$SRC_URL" "$ZSTD_SHA256"
EOF

FROM builder AS libffi
ARG LIBFFI_VERSION
ARG LIBFFI_SHA256
ENV SRC_URL=https://github.com/libffi/libffi/releases/download/v${LIBFFI_VERSION}/libffi-${LIBFFI_VERSION}.tar.gz
ADD --checksum=sha256:${LIBFFI_SHA256} ${SRC_URL} /tmp/src.tar.gz
RUN --mount=type=cache,id=ccache,target=/root/.cache/ccache <<EOF
set -eux
# Upstream builds with -O3, but setting CFLAGS replaces its default.
export CFLAGS="$(echo "$CFLAGS" | sed 's/-O2/-O3/')"
tar -xf /tmp/src.tar.gz --strip-components=1
./configure --prefix=/usr/local --disable-static --disable-docs \
    --disable-multi-os-directory
make -j"$(nproc)"
make DESTDIR=/out install
split-runtime
install-licenses libffi /build/LICENSE
package-metadata libffi "$LIBFFI_VERSION" MIT libffi_project:libffi "$SRC_URL" "$LIBFFI_SHA256"
EOF

FROM builder AS sqlite
ARG SQLITE_VERSION
ARG SQLITE_YEAR
ARG SQLITE_SHA256
ENV SRC_URL=https://www.sqlite.org/${SQLITE_YEAR}/sqlite-autoconf-${SQLITE_VERSION}.tar.gz
ADD --checksum=sha256:${SQLITE_SHA256} ${SRC_URL} /tmp/src.tar.gz
RUN --mount=type=cache,id=ccache,target=/root/.cache/ccache <<EOF
set -eux
tar -xf /tmp/src.tar.gz --strip-components=1
./configure --prefix=/usr/local --disable-static --disable-readline \
    --soname=legacy \
    --enable-fts4 --enable-fts5 --enable-rtree --enable-session \
    --enable-dbstat --enable-column-metadata
make -j"$(nproc)"
make DESTDIR=/out install
split-runtime
# SQLite is public domain; ship the dedication from the top of sqlite3.h.
# (Up to the line of asterisks. sed, not awk: Debian's mawk has no {n,}.)
sed '/^[*][*][*][*][*][*][*][*][*][*]/,$d' /build/sqlite3.h > /build/PUBLIC-DOMAIN
install-licenses sqlite /build/PUBLIC-DOMAIN
v=$SQLITE_VERSION; version="$((v/1000000)).$((v/10000%100)).$((v/100%100))"
package-metadata sqlite "$version" blessing sqlite:sqlite "$SRC_URL" "$SQLITE_SHA256"
EOF

FROM builder AS expat
ARG EXPAT_VERSION
ARG EXPAT_SHA256
ENV SRC_URL=https://github.com/libexpat/libexpat/releases/download/R_${EXPAT_VERSION//./_}/expat-${EXPAT_VERSION}.tar.xz
ADD --checksum=sha256:${EXPAT_SHA256} ${SRC_URL} /tmp/src.tar.xz
RUN --mount=type=cache,id=ccache,target=/root/.cache/ccache <<EOF
set -eux
tar -xf /tmp/src.tar.xz --strip-components=1
./configure --prefix=/usr/local --disable-static --without-docbook \
    --without-examples --without-tests --without-xmlwf
make -j"$(nproc)"
make DESTDIR=/out install
split-runtime
install-licenses expat /build/COPYING
package-metadata expat "$EXPAT_VERSION" MIT libexpat_project:libexpat "$SRC_URL" "$EXPAT_SHA256"
EOF

FROM builder AS mpdecimal
ARG MPDECIMAL_VERSION
ARG MPDECIMAL_SHA256
ENV SRC_URL=https://www.bytereef.org/software/mpdecimal/releases/mpdecimal-${MPDECIMAL_VERSION}.tar.gz
ADD --checksum=sha256:${MPDECIMAL_SHA256} ${SRC_URL} /tmp/src.tar.gz
RUN --mount=type=cache,id=ccache,target=/root/.cache/ccache <<EOF
set -eux
tar -xf /tmp/src.tar.gz --strip-components=1
./configure --prefix=/usr/local --disable-static --disable-cxx --disable-doc
make -j"$(nproc)"
make DESTDIR=/out install
split-runtime
install-licenses mpdecimal /build/COPYRIGHT.txt
package-metadata mpdecimal "$MPDECIMAL_VERSION" BSD-2-Clause - "$SRC_URL" "$MPDECIMAL_SHA256"
EOF

FROM builder AS ncurses
ARG NCURSES_VERSION
ARG NCURSES_SHA256
ENV SRC_URL=https://ftp.gnu.org/gnu/ncurses/ncurses-${NCURSES_VERSION}.tar.gz
ADD --checksum=sha256:${NCURSES_SHA256} ${SRC_URL} /tmp/src.tar.gz
RUN --mount=type=cache,id=ccache,target=/root/.cache/ccache <<EOF
set -eux
tar -xf /tmp/src.tar.gz --strip-components=1
# Wide-character ncursesw with terminfo built into the main library. Our own
# terminfo directory is searched first, then the usual system locations.
./configure --prefix=/usr/local --with-shared --without-normal \
    --without-debug --without-ada --without-cxx-binding --without-manpages \
    --without-tests --enable-widec --enable-pc-files \
    --with-pkg-config-libdir=/usr/local/lib/pkgconfig \
    --with-default-terminfo-dir=/usr/local/share/terminfo \
    --with-terminfo-dirs=/usr/local/share/terminfo:/etc/terminfo:/lib/terminfo:/usr/share/terminfo
make -j"$(nproc)"
make DESTDIR=/out install
split-runtime
# Ship only a handful of common terminal definitions instead of the full
# (multi-MB) database; the system terminfo dirs are used as a fallback.
cd /runtime/usr/local/share
mv terminfo terminfo.full
for t in dumb linux vt100 vt220 xterm xterm-256color xterm-color \
         screen screen-256color tmux tmux-256color; do
  f=$(cd terminfo.full && echo */$t)
  install -Dm644 terminfo.full/$f terminfo/$f
done
rm -rf terminfo.full tabset
install-licenses ncurses /build/COPYING
package-metadata ncurses "$NCURSES_VERSION" X11-distribute-modifications-variant gnu:ncurses "$SRC_URL" "$NCURSES_SHA256"
EOF

FROM builder AS readline
ARG READLINE_VERSION
ARG READLINE_SHA256
COPY --from=ncurses /out/ /
ENV SRC_URL=https://ftp.gnu.org/gnu/readline/readline-${READLINE_VERSION}.tar.gz
ADD --checksum=sha256:${READLINE_SHA256} ${SRC_URL} /tmp/src.tar.gz
# GNU ships readline fixes as numbered patches (readline83-001, ...) rather
# than new tarballs. scripts/update-deps.py downloads and GPG-verifies them
# into patches/readline/; apply the ones for this version, in order.
RUN --mount=type=cache,id=ccache,target=/root/.cache/ccache \
    --mount=type=bind,source=patches/readline,target=/patches <<EOF
set -eux
tar -xf /tmp/src.tar.gz --strip-components=1
for p in /patches/readline$(echo "$READLINE_VERSION" | tr -d .)-[0-9][0-9][0-9]; do
  [ -e "$p" ] || continue
  patch -p0 --forward < "$p"
done
./configure --prefix=/usr/local --disable-static --disable-install-examples \
    --with-curses
# Link libreadline.so against ncursesw explicitly so consumers don't need to.
make -j"$(nproc)" SHLIB_LIBS="-L/usr/local/lib -lncursesw"
make DESTDIR=/out install
split-runtime
install-licenses readline /build/COPYING
# The patches applied above are part of its Corresponding Source.
mkdir -p /runtime/usr/local/share/licenses/readline/patches
for p in /patches/readline$(echo "$READLINE_VERSION" | tr -d .)-[0-9][0-9][0-9]; do
  [ -e "$p" ] || continue
  install -m644 "$p" /runtime/usr/local/share/licenses/readline/patches/
done
package-metadata readline "$READLINE_VERSION" GPL-3.0-or-later gnu:readline "$SRC_URL" "$READLINE_SHA256"
EOF

FROM builder AS gdbm
ARG GDBM_VERSION
ARG GDBM_SHA256
ENV SRC_URL=https://ftp.gnu.org/gnu/gdbm/gdbm-${GDBM_VERSION}.tar.gz
ADD --checksum=sha256:${GDBM_SHA256} ${SRC_URL} /tmp/src.tar.gz
RUN --mount=type=cache,id=ccache,target=/root/.cache/ccache <<EOF
set -eux
tar -xf /tmp/src.tar.gz --strip-components=1
# libgdbm_compat provides the ndbm API used by the dbm.ndbm module.
./configure --prefix=/usr/local --disable-static --disable-nls \
    --enable-libgdbm-compat --without-readline
make -j"$(nproc)"
make DESTDIR=/out install
split-runtime
install-licenses gdbm /build/COPYING
package-metadata gdbm "$GDBM_VERSION" GPL-3.0-or-later - "$SRC_URL" "$GDBM_SHA256"
EOF

FROM builder AS libuuid
ARG UTIL_LINUX_VERSION
ARG UTIL_LINUX_SHA256
ENV SRC_URL=https://www.kernel.org/pub/linux/utils/util-linux/v${UTIL_LINUX_VERSION%.*}/util-linux-${UTIL_LINUX_VERSION}.tar.xz
ADD --checksum=sha256:${UTIL_LINUX_SHA256} ${SRC_URL} /tmp/src.tar.xz
RUN --mount=type=cache,id=ccache,target=/root/.cache/ccache <<EOF
set -eux
tar -xf /tmp/src.tar.xz --strip-components=1
./configure --prefix=/usr/local --disable-all-programs --enable-libuuid \
    --disable-static --disable-nls --disable-bash-completion --disable-poman \
    --without-python --without-systemd --without-udev
make -j"$(nproc)"
# vex/util-linux.openvex.json relies on this stage compiling nothing but
# libuuid: its own sources (libuuid/) plus the lib/ helpers it links in,
# which libtool builds with a libuuid_la- prefix. In particular, none of
# login-utils/ or sys-utils/ (login, mount, ...) may be compiled.
other=$(find . -name '*.o' ! -path './libuuid/*' ! -name 'libuuid_la-*')
if [ -n "$other" ]; then
  echo "util-linux compiled more than libuuid:" $other >&2
  exit 1
fi
make DESTDIR=/out install
split-runtime
install-licenses util-linux /build/libuuid/COPYING /build/Documentation/licenses/COPYING.BSD-3-Clause
package-metadata util-linux "$UTIL_LINUX_VERSION" BSD-3-Clause kernel:util-linux "$SRC_URL" "$UTIL_LINUX_SHA256"
EOF


# =============================================================================
# CPython
# =============================================================================
FROM builder AS python
ARG PYTHON_VERSION
ARG PYTHON_SHA256

# Fetch and unpack the source before pulling in the dependencies, so a
# dependency bump doesn't redo this step. (Same for the build-option ARGs
# below: every RUN after an ARG is invalidated when its value changes, used
# or not.)
ENV SRC_URL=https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz
ADD --checksum=sha256:${PYTHON_SHA256} ${SRC_URL} /tmp/src.tar.xz
RUN tar -xf /tmp/src.tar.xz --strip-components=1 && rm /tmp/src.tar.xz

COPY --from=openssl   /out/ /
COPY --from=zlib      /out/ /
COPY --from=bzip2     /out/ /
COPY --from=xz        /out/ /
COPY --from=zstd      /out/ /
COPY --from=libffi    /out/ /
COPY --from=sqlite    /out/ /
COPY --from=expat     /out/ /
COPY --from=mpdecimal /out/ /
COPY --from=ncurses   /out/ /
COPY --from=readline  /out/ /
COPY --from=gdbm      /out/ /
COPY --from=libuuid   /out/ /

ARG PYTHON_OPTIMIZE
ARG PYTHON_CONFIGURE_EXTRA
RUN --mount=type=cache,id=ccache,target=/root/.cache/ccache <<EOF
set -eux
ldconfig
opt=""
if [ "$PYTHON_OPTIMIZE" = "true" ]; then
  opt="--enable-optimizations --with-lto"
fi
# CPython chooses its own optimization level (-O3, in OPT); the builder's -O2
# would come later on the command line and override it.
export CFLAGS="$(echo "$CFLAGS" | sed 's/-O2 *//')"
# py_cv_module__tkinter=n/a: never build tkinter, even if Tk is found.
./configure \
    --prefix=/usr/local \
    --enable-shared \
    --enable-loadable-sqlite-extensions \
    --with-openssl=/usr/local \
    --with-system-expat \
    --with-system-libmpdec \
    --with-readline=readline \
    --with-ensurepip=no \
    --without-static-libpython \
    --disable-test-modules \
    py_cv_module__tkinter=n/a \
    $opt $PYTHON_CONFIGURE_EXTRA
make -j"$(nproc)"

# Fail the build if any optional stdlib extension module could not be built
# (other than the ones deliberately disabled above).
LD_LIBRARY_PATH=$PWD ./python -I Tools/build/check_extension_modules.py --strict

make DESTDIR=/out install

cd /out/usr/local
# Drop the Tk-based front-end pieces (and anything that only makes sense with
# Tk), plus _ios_support, which only works on Apple platforms.
pyver="${PYTHON_VERSION%.*}"
rm -rf lib/python$pyver/tkinter lib/python$pyver/idlelib \
       lib/python$pyver/turtledemo lib/python$pyver/turtle.py \
       lib/python$pyver/__pycache__/turtle.* \
       lib/python$pyver/_ios_support.py \
       lib/python$pyver/__pycache__/_ios_support.* \
       bin/idle3*
# Unversioned convenience names, as in the official python images.
ln -sf python3 bin/python
ln -sf pydoc3 bin/pydoc
ln -sf python3-config bin/python-config
EOF

# Runtime layout: Python keeps its headers and python3-config (needed to
# build C extensions with pip if a compiler is added), but loses man pages,
# static archives, and debug symbols.
RUN <<EOF
set -eux
rm -rf /runtime
cp -a /out /runtime
cd /runtime/usr/local
rm -rf share/man
find . -name '*.a' -delete
strip --strip-unneeded bin/python3.* lib/libpython3*.so.* || true
find lib -name '*.so' -type f -exec strip --strip-unneeded {} +
install-licenses python /build/LICENSE
package-metadata python "$PYTHON_VERSION" PSF-2.0 python:python "$SRC_URL" "$PYTHON_SHA256"
EOF


# =============================================================================
# pip, installed into its own tree so the runtime image gets it as one layer.
# Only this stage reruns on a pip bump; CPython is not rebuilt.
# =============================================================================
FROM scratch AS pip-wheel
ARG PIP_VERSION
ARG PIP_SHA256
ADD --checksum=sha256:${PIP_SHA256} \
    https://files.pythonhosted.org/packages/py3/p/pip/pip-${PIP_VERSION}-py3-none-any.whl \
    /pip-${PIP_VERSION}-py3-none-any.whl

FROM python AS pip
ARG PIP_VERSION
RUN --mount=from=pip-wheel,target=/wheels <<EOF
set -eux
cp -a /runtime/. /
whl=/wheels/pip-${PIP_VERSION}-py3-none-any.whl
python3 $whl/pip install --root /pip-out --no-cache-dir --no-index \
    --disable-pip-version-check --root-user-action=ignore $whl
ln -sf pip3 /pip-out/usr/local/bin/pip
EOF


# =============================================================================
# CA certificates. Replaces the bundle the runtime base image ships (which is
# also where its SSL_CERT_FILE points), so there is one bundle, versioned here.
# =============================================================================
FROM builder AS cacert
ARG CACERT_DATE
ARG CACERT_SHA256
ADD --chmod=0644 --checksum=sha256:${CACERT_SHA256} \
    https://curl.se/ca/cacert-${CACERT_DATE}.pem \
    /out/etc/ssl/certs/ca-certificates.crt
# OpenSSL's default CA file for --openssldir=/etc/ssl, for when SSL_CERT_FILE
# isn't set.
RUN ln -s certs/ca-certificates.crt /out/etc/ssl/cert.pem && \
    mkdir -p /out/usr/local/share/licenses/cacert && \
    printf '%s\n' \
      "The CA bundle (/etc/ssl/certs/ca-certificates.crt) is extracted from Mozilla's" \
      "certificate store and is subject to the Mozilla Public License, v. 2.0, as its" \
      "header states: https://mozilla.org/MPL/2.0/" \
      > /out/usr/local/share/licenses/cacert/NOTICE && \
    ROOT=/out package-metadata cacert "$CACERT_DATE" MPL-2.0 - \
      "https://curl.se/ca/cacert-${CACERT_DATE}.pem" "$CACERT_SHA256"


# =============================================================================
# system-libs: pieces taken as-is from the Debian build image (so they track
# BUILD_IMAGE's digest rather than having their own version ARGs).
#
# - libgcc_s.so.1 and libstdc++.so.6: the manylinux policy (PEP 600) treats
#   both as system libraries that wheels may link against but must not
#   bundle, so C/C++ wheels from PyPI expect the OS to provide them.
# - ldconfig (statically linked) and Debian's ld.so.conf, which lists
#   /usr/local/lib: ctypes.util.find_library() runs `/sbin/ldconfig -p`
#   (directly, no shell) and reads the cache it prints.
#
# Each package's dpkg record goes into /var/lib/dpkg/status.d/ (the layout
# distroless uses), with md5sums for only the files copied, so Trivy, Syft
# and Grype see them as the Debian packages they are.
# =============================================================================
FROM ${BUILD_IMAGE} AS system-libs
RUN <<EOF
set -eux
status=/out/var/lib/dpkg/status.d
mkdir -p $status /out/var/cache/ldconfig
take() {  # take PACKAGE FILE...
  pkg=$1; shift
  dpkg-query -s $pkg > $status/$pkg
  for f in "$@"; do
    mkdir -p /out$(dirname $f)
    cp -a $f /out$f
    [ -L $f ] || (cd /out && md5sum ${f#/}) >> $status/$pkg.md5sums
  done
}
take libgcc-s1 $(dpkg -L libgcc-s1 | grep -E '^/usr/lib/.*\.so\.[0-9.]+$')
take libstdc++6 $(dpkg -L libstdc++6 | grep -E '^/usr/lib/.*\.so\.[0-9.]+$')
take libc-bin /usr/sbin/ldconfig /etc/ld.so.conf /etc/ld.so.conf.d/libc.conf
# Their license texts, where distroless keeps them for its own packages.
for pkg in libgcc-s1 libstdc++6 libc-bin; do
  install -Dm644 "$(readlink -f /usr/share/doc/$pkg/copyright)" /out/usr/share/doc/$pkg/copyright
done
EOF


# =============================================================================
# runtime: the final image
# =============================================================================
# The runtime base has no shell, so this stage is COPY-only: everything is
# prepared in the build stages. Every COPY is its own layer and uses --link,
# so a dependency bump only replaces that dependency's layer plus the CPython
# and pip layers; all other layers are reused as-is (and stay cached in
# registries).
#
# Every library and extension module carries an rpath of /usr/local/lib, so
# loading never depends on the ld.so.cache; the cache only exists for
# ctypes.util.find_library().
FROM ${RUNTIME_IMAGE} AS runtime

ENV LANG=C.UTF-8

COPY --link --from=system-libs /out/   /
COPY --link --from=cacert    /out/     /
COPY --link --from=openssl   /runtime/ /
COPY --link --from=zlib      /runtime/ /
COPY --link --from=bzip2     /runtime/ /
COPY --link --from=xz        /runtime/ /
COPY --link --from=zstd      /runtime/ /
COPY --link --from=libffi    /runtime/ /
COPY --link --from=sqlite    /runtime/ /
COPY --link --from=expat     /runtime/ /
COPY --link --from=mpdecimal /runtime/ /
COPY --link --from=ncurses   /runtime/ /
COPY --link --from=readline  /runtime/ /
COPY --link --from=gdbm      /runtime/ /
COPY --link --from=libuuid   /runtime/ /
COPY --link --from=python    /runtime/ /
COPY --link --from=pip       /pip-out/ /

# Licenses and sources for everything in the image. The release workflow
# passes the repository and commit, which hold the Dockerfile and patches.
ARG SOURCE_REPOSITORY="(not recorded: local build)"
ARG SOURCE_REVISION="(not recorded: local build)"
COPY --link <<EOF /usr/local/share/licenses/README
Licenses and sources of the software in this image
==================================================

Each directory here is one component built from source into /usr/local:

  <component>/          its license text(s)
  <component>/SOURCE    the exact upstream source archive it was built from:
                        download URL and SHA-256
  readline/patches/     the official GNU patches applied to readline

GPL-licensed components (readline, gdbm): their Corresponding Source is the
archive named in their SOURCE file, plus the patches in readline/patches and
the build scripts below.

Build scripts (Dockerfile) and patches:
  ${SOURCE_REPOSITORY}
  at commit ${SOURCE_REVISION}

pip and its vendored packages: their licenses are in
  /usr/local/lib/python3.*/site-packages/pip-*.dist-info/licenses/

Files from the Debian-based distroless image and the Debian build image
(glibc, libgcc, libstdc++, ldconfig, tzdata, ...): /usr/share/doc/<package>/
copyright. Their sources are at https://sources.debian.org/.

Machine-readable SBOMs of the components above: /usr/local/share/sbom/
EOF

# Build /etc/ld.so.cache. Exec form, since there is no shell. This is the only
# layer that changes on every dependency bump, and it's a few KB.
RUN ["/sbin/ldconfig"]

CMD ["python3"]


# =============================================================================
# test: smoke tests against the runtime image (docker buildx bake test)
# =============================================================================
FROM runtime AS test
ARG PYTHON_VERSION
ARG OPENSSL_VERSION
ARG ZLIB_VERSION
ARG BZIP2_VERSION
ARG XZ_VERSION
ARG ZSTD_VERSION
ARG SQLITE_VERSION
ARG EXPAT_VERSION
ARG MPDECIMAL_VERSION
ARG NCURSES_VERSION
ARG READLINE_VERSION
ARG GDBM_VERSION
ARG PIP_VERSION
ARG LIBFFI_VERSION
ARG UTIL_LINUX_VERSION
ARG CACERT_DATE
ARG X86_64_CFLAGS
ARG X86_64_LDFLAGS
ARG PYTHON_OPTIMIZE
COPY tests/ /tests/
# Exec form: there is no /bin/sh in the runtime image.
RUN ["python3", "-I", "/tests/check_libs.py"]
RUN ["python3", "-I", "/tests/smoke_test.py"]
RUN ["python3", "-I", "/tests/check_metadata.py"]


# The default target is the runtime image.
FROM runtime
