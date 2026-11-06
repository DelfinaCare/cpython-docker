#!/usr/bin/env bash
# Prints GitHub Actions outputs describing what to build, taken from the
# `python` group in docker-bake.hcl so that file stays the single source of
# truth for CPython versions and platforms:
#
#   pythons=[{"minor":"3.14","version":"3.14.10","sha256":"..."}, ...]
#   matrix={"include":[{<python fields>,"platform":"linux/amd64","arch":"amd64",
#                       "runner":"ubuntu-24.04"}, ...]}
#
# Each platform builds natively on its own runner (no QEMU).
set -euo pipefail

bake=$(docker buildx bake --print python)

pythons=$(jq -c '
  [.target[].args | {
      minor: (.PYTHON_VERSION | split(".")[:2] | join(".")),
      version: .PYTHON_VERSION,
      sha256: .PYTHON_SHA256}]
  | sort_by(.minor | split(".") | map(tonumber))' <<<"$bake")

matrix=$(jq -c --argjson pythons "$pythons" '
  {"linux/amd64": "ubuntu-24.04", "linux/arm64": "ubuntu-24.04-arm"} as $runners
  | ([.target[].platforms[]] | unique) as $platforms
  | {include: [$pythons[] as $py | $platforms[] as $p
      | $py + {platform: $p,
               arch: ($p | split("/")[1]),
               runner: ($runners[$p] // error("no runner for \($p)"))}]}' <<<"$bake")

echo "pythons=$pythons"
echo "matrix=$matrix"
