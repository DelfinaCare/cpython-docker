#!/usr/bin/env bash
# Prints GitHub Actions outputs describing what to build, taken from the
# `python` group in docker-bake.hcl so that file stays the single source of
# truth for CPython versions and platforms:
#
#   pythons=[{"minor":"3.14","version":"3.14.10","sha256":"..."}, ...]
#   merges=[{"minor":"3.14","version":"3.14.10","variant":"shell","suffix":"-shell"}, ...]
#   builds={"include":[{<python fields>,"platform":"linux/amd64","arch":"amd64",
#                       "runner":"ubuntu-24.04"}, ...]}
#   matrix={"include":[{<python fields>,"variant":"release"|"shell","suffix":""|"-shell",
#                       "platform":"linux/amd64","arch":"amd64",
#                       "runner":"ubuntu-24.04"}, ...]}
#
# Every CPython version is built as the shell-less release image and as the
# -shell image (variants come from RUNTIME_VARIANT in the bake targets).
# `matrix` has one entry per variant (the Test workflow); `builds` has one per
# CPython version x platform (the Release workflow, which builds both variants
# in one job so the -shell image reuses the release image's exact layers).
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
               target: (if $v == "release" then "runtime" else "runtime-\($v)" end),
               suffix: (if $v == "release" then "" else "-\($v)" end),
               platform: $p,
               arch: ($p | split("/")[1]),
               runner: ($runners[$p] // error("no runner for \($p)"))}]}' <<<"$bake")

# One entry per image to tag: every CPython version x variant.
merges=$(jq -c '[.include | group_by([.minor, .variant])[] | .[0]
  | {minor, version, variant, suffix}]' <<<"$matrix")

# One entry per CPython version x platform (both variants).
builds=$(jq -c '{include: [.include[] | select(.variant == "release")
  | del(.variant, .target, .suffix)]}' <<<"$matrix")

echo "pythons=$pythons"
echo "merges=$merges"
echo "builds=$builds"
echo "matrix=$matrix"
