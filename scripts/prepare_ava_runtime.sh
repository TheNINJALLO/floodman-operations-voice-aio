#!/usr/bin/env bash
set -euo pipefail

# Pterodactyl can run container images with a read-only root filesystem. AVA's
# upstream layout expects its config and models inside the project tree, so this
# creates a writable AVA worktree under Floodman's persistent data volume.

DATA_DIR="${DATA_DIR:-/home/container/data}"
CONFIG_DIR="${CONFIG_DIR:-${DATA_DIR}/config}"
AVA_IMAGE_DIR="${AVA_IMAGE_DIR:-/opt/ava}"
AVA_RUNTIME_DIR="${AVA_RUNTIME_DIR:-${DATA_DIR}/runtime/ava}"

python3 - "${DATA_DIR}" "${AVA_RUNTIME_DIR}" <<'PY'
from pathlib import Path
import sys

data_dir = Path(sys.argv[1]).resolve()
runtime_dir = Path(sys.argv[2]).resolve(strict=False)
if runtime_dir == data_dir or data_dir not in runtime_dir.parents:
    raise SystemExit(
        f"AVA_RUNTIME_DIR must be a child of DATA_DIR; got {runtime_dir} outside {data_dir}"
    )
PY

if [[ ! -f "${AVA_IMAGE_DIR}/main.py" ]]; then
  echo "AVA image source is missing ${AVA_IMAGE_DIR}/main.py" >&2
  exit 1
fi
if [[ ! -d "${AVA_IMAGE_DIR}/local_ai_server" ]]; then
  echo "AVA image source is missing ${AVA_IMAGE_DIR}/local_ai_server" >&2
  exit 1
fi

runtime_parent="$(dirname "${AVA_RUNTIME_DIR}")"
staging_dir="${AVA_RUNTIME_DIR}.staging.$$"
mkdir -p "${runtime_parent}" "${DATA_DIR}/models"

# cp -a preserves the source tree's read-only modes. Before deleting a copied
# tree, restore owner write/search permission on directories and write
# permission on files. Symlinks are unlinked without traversing their targets.
remove_tree() {
  local target="$1"
  if [[ -L "${target}" ]]; then
    rm -f -- "${target}"
  elif [[ -e "${target}" ]]; then
    chmod -R u+rwX "${target}" 2>/dev/null || true
    rm -rf -- "${target}"
  fi
}

remove_tree "${staging_dir}"

cleanup() {
  remove_tree "${staging_dir}"
}
trap cleanup EXIT

# Copy from the immutable image layer into a writable persistent worktree. A
# staging directory prevents a half-copied runtime after an interrupted boot.
mkdir -p "${staging_dir}"
cp -a "${AVA_IMAGE_DIR}/." "${staging_dir}/"
chmod -R u+rwX "${staging_dir}" 2>/dev/null || true
remove_tree "${staging_dir}/.git"

remove_tree "${AVA_RUNTIME_DIR}"
mv "${staging_dir}" "${AVA_RUNTIME_DIR}"
trap - EXIT

mkdir -p "${AVA_RUNTIME_DIR}/config"
if [[ -f "${CONFIG_DIR}/ava/ai-agent.local.yaml" ]]; then
  cp "${CONFIG_DIR}/ava/ai-agent.local.yaml" \
    "${AVA_RUNTIME_DIR}/config/ai-agent.local.yaml"
fi

# Seed the persistent model registry before replacing the runtime models folder
# with a link to persistent writable storage.
if [[ ! -f "${DATA_DIR}/models/registry.json" && -f "${AVA_RUNTIME_DIR}/models/registry.json" ]]; then
  cp "${AVA_RUNTIME_DIR}/models/registry.json" "${DATA_DIR}/models/registry.json"
fi
remove_tree "${AVA_RUNTIME_DIR}/models"
ln -s "${DATA_DIR}/models" "${AVA_RUNTIME_DIR}/models"

printf '%s\n' "${AVA_IMAGE_DIR}" > "${AVA_RUNTIME_DIR}/.floodman-image-source"
printf 'AVA writable runtime prepared at %s\n' "${AVA_RUNTIME_DIR}"
