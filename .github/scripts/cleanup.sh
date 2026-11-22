#!/usr/bin/env bash
# Deletes images left by failed release attempts once they're older than
# $DAYS days (7 by default), so a failed build can still be inspected for a
# week. Run by .github/workflows/cleanup.yml; with DRY_RUN=true it only
# prints what it would delete.
#
# A package version is deleted when it was last updated more than $DAYS days
# ago and:
#
#   <image>-staging  it's tagged. Staging only holds a release's images until
#                    `release` copies them (same digests) to the real package,
#                    so anything older is a failed attempt or already copied.
#   <image>          every one of its tags is a release tag, like
#                    3.14.10-2026.11.21 or 3.14.10-shell-2026.11.21-1, whose
#                    release (v2026.11.21) was never published: `release`
#                    copied the images but failed before publishing. An image
#                    with any other tag (3.14, 3.14.10, ...) or with a
#                    published release's tag is kept.
#   either           it's untagged and no kept image refers to it: the
#                    per-platform images and attestations of a deleted image,
#                    a build whose attempt failed before `merge`, or an image
#                    whose tag a retry moved to its own image.
#
# A multi-arch image's platform images and attestations are untagged versions
# of their own, so they're only deleted if no kept image refers to them. Each
# package keeps at least its newest tagged version (GitHub refuses to delete
# a package's last tagged version).
#
# Needs GH_TOKEN (packages: write) and `docker login ghcr.io`.
set -euo pipefail

: "${GITHUB_REPOSITORY:?}"
DAYS=${DAYS:-7}
DRY_RUN=${DRY_RUN:-false}

owner=${GITHUB_REPOSITORY%%/*}
name=${GITHUB_REPOSITORY#*/}
name=${name,,}
cutoff=$(date -u -d "-${DAYS} days" +%Y-%m-%dT%H:%M:%SZ)

# /users/<owner> also answers for organizations, and says which it is.
if [ "$(gh api "users/${owner}" --jq .type)" = Organization ]; then
  packages="orgs/${owner}/packages/container"
else
  packages="users/${owner}/packages/container"
fi

released=$(gh api --paginate "repos/${GITHUB_REPOSITORY}/releases" \
             --jq '[.[] | select(.draft | not) | .tag_name]' | jq -sc 'add // []')
echo "Deleting versions last updated before ${cutoff}"
echo "Published releases: ${released}"

failed=0

# clean <package> <staging|real>
clean() {
  local package=$1 kind=$2 ref="ghcr.io/${owner,,}/$1"

  if ! gh api "${packages}/${package}" --silent; then
    echo "::warning::${packages}/${package} doesn't exist or this token can't read it; skipping"
    return
  fi

  # One line per version: {id, digest, updated, tags, delete}, where delete
  # says whether a tagged version goes (untagged ones are decided below).
  local versions
  versions=$(gh api --paginate "${packages}/${package}/versions" \
               --jq '.[] | {id, digest: .name, updated: .updated_at,
                            tags: .metadata.container.tags}' |
             jq -sc --arg kind "$kind" --arg cutoff "$cutoff" --argjson released "$released" '
      def unreleased:
        (first(capture("-(?<r>[0-9]{4}\\.[0-9]{2}\\.[0-9]{2}(-[0-9]+)?)$") | .r) // null) as $r
        | $r != null and (("v" + $r) | IN($released[]) | not);
      map(. + {delete: ((.tags | length) > 0 and .updated < $cutoff
                        and ($kind == "staging" or all(.tags[]; unreleased)))})
      # GitHub refuses to delete the last tagged version, so keep the newest.
      | if any(.[]; (.tags | length) > 0 and (.delete | not)) then .
        else (map(select((.tags | length) > 0)) | max_by(.updated) | .id) as $newest
          | map(if .id == $newest then .delete = false else . end)
        end
      | .[]')

  # Every digest a kept tagged version refers to, however deep (a multi-arch
  # image lists its platform images and attestations, which may be indexes
  # themselves). Fails the run rather than miss one.
  local -A keep=()
  walk() {
    local digest=$1 raw child type
    [ -n "${keep[$digest]:-}" ] && return
    keep[$digest]=1
    if ! raw=$(docker buildx imagetools inspect --raw "${ref}@${digest}"); then
      echo "::error::Couldn't read ${ref}@${digest}; deleting nothing from ${package}"
      exit 1
    fi
    while read -r child type; do
      [ -n "$child" ] || continue
      case $type in
        *index* | *manifest.list*) walk "$child" ;;
        *) keep[$child]=1 ;;
      esac
    done < <(jq -r '.manifests[]? | "\(.digest) \(.mediaType)"' <<<"$raw")
  }
  while read -r digest; do
    walk "$digest"
  done < <(jq -r 'select((.tags | length) > 0 and (.delete | not)) | .digest' <<<"$versions")

  local id digest updated tags deleted=0 kept=0
  while IFS=$'\t' read -r id digest updated tags; do
    if [ "$tags" = "" ]; then
      if [ -n "${keep[$digest]:-}" ] || [[ ! $updated < $cutoff ]]; then
        kept=$((kept + 1))
        continue
      fi
      tags="(untagged)"
    fi
    echo "Deleting ${ref}@${digest} ${tags} (updated ${updated})"
    deleted=$((deleted + 1))
    [ "$DRY_RUN" = true ] && continue
    if ! gh api -X DELETE "${packages}/${package}/versions/${id}" --silent; then
      echo "::warning::Couldn't delete ${ref}@${digest} (version ${id})"
      failed=1
    fi
  done < <(jq -r '
      select(.delete or (.tags | length) == 0)
      | [.id, .digest, .updated, (.tags | join(","))] | @tsv' <<<"$versions")
  kept=$((kept + $(jq -s 'map(select((.tags | length) > 0 and (.delete | not))) | length' <<<"$versions")))

  local verb=Deleted
  [ "$DRY_RUN" = true ] && verb="Would delete"
  echo "${verb} ${deleted} versions of ${ref}; kept ${kept}" | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
}

clean "${name}-staging" staging
clean "${name}" real

exit "$failed"
