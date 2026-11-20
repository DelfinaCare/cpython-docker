#!/usr/bin/env bash
# Prints GitHub Actions outputs describing what to build, taken from the
# `python` group in docker-bake.hcl so that file stays the single source of
# truth for CPython versions and platforms:
#
#   pythons=[{"minor":"3.14","version":"3.14.10","sha256":"..."}, ...]
#   merges=[{"minor":"3.14","version":"3.14.10","variant":"debug","suffix":"-dbg"}, ...]
#   matrix={"include":[{<python fields>,"variant":"release"|"debug","suffix":""|"-dbg",
#                       "platform":"linux/amd64","arch":"amd64",
#                       "runner":"ubuntu-24.04"}, ...]}
#
# Every CPython version is built as the shell-less release image and as the
# -dbg image (variants come from RUNTIME_VARIANT in the bake targets).
#
# Each platform builds natively on its own runner (no QEMU).
set -euo pipefail

bake=$(docker buildx bake --print python)

pythons=$(jq -c '
  [.target[].args | select(.RUNTIME_VARIANT == "release") | {
      minor: (.PYTHON_VERSION | split(".")[:2] | join(".")),
      version: .PYTHON_VERSION,
      sha256: .PYTHON_SHA256}]
  | sort_by(.minor | split(".") | map(tonumber))' <<<"$bake")

matrix=$(jq -c --argjson pythons "$pythons" '
  {"linux/amd64": "ubuntu-24.04", "linux/arm64": "ubuntu-24.04-arm"} as $runners
  | ([.target[].platforms[]] | unique) as $platforms
  | ([.target[].args.RUNTIME_VARIANT] | unique) as $variants
  | {include: [$pythons[] as $py | $variants[] as $v | $platforms[] as $p
      | $py + {variant: $v,
               suffix: (if $v == "release" then "" else "-dbg" end),
               platform: $p,
               arch: ($p | split("/")[1]),
               runner: ($runners[$p] // error("no runner for \($p)"))}]}' <<<"$bake")

# One entry per image to tag: every CPython version x variant.
merges=$(jq -c '[.include | group_by([.minor, .variant])[] | .[0]
  | {minor, version, variant, suffix}]' <<<"$matrix")

echo "pythons=$pythons"
echo "merges=$merges"
echo "matrix=$matrix"
