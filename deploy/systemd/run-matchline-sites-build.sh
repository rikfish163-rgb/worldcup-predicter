#!/usr/bin/env bash

set -u

runtime_dir="${MATCHLINE_RUNTIME_DIR:-/media/hetaisheng/044A81D94A81C83E/soccerdata-live-runtime}"
project_dir="/home/hetaisheng/soccerdata/matchline_sites"
# Keep Wrangler/Miniflare's disposable state off the repository by default.
# Operators can still provide a dedicated writable state directory explicitly.
export MATCHLINE_LOCAL_STATE_DIR="${MATCHLINE_LOCAL_STATE_DIR:-/dev/shm/matchline-sites-build-state}"
# The dependency tree is intentionally kept on the external volume, but that
# volume may be mounted read-only. Vite writes a tiny `.vite-temp` directory
# while loading an ESM config, so create a reversible ext4 symlink mirror for
# the duration of this build instead of copying the ~800 MB dependency tree.
node_modules_dir="${project_dir}/node_modules"
node_modules_restore_path="${project_dir}/.node_modules-external-link.$$"
node_modules_overlay=0

restore_node_modules() {
  if [ "${node_modules_overlay}" -ne 1 ]; then
    return 0
  fi
  # This directory was created by this wrapper and contains only symlinks plus
  # Vite's disposable cache. Keep the target explicit and restore the original
  # link even when the build exits non-zero or receives a signal.
  if [ -d "${node_modules_dir}" ] && [ ! -L "${node_modules_dir}" ]; then
    find "${node_modules_dir}" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    rmdir "${node_modules_dir}"
  fi
  if [ -L "${node_modules_restore_path}" ]; then
    mv -f -- "${node_modules_restore_path}" "${node_modules_dir}"
  fi
  node_modules_overlay=0
}

prepare_node_modules_overlay() {
  if [ ! -L "${node_modules_dir}" ]; then
    return 0
  fi
  local external_node_modules
  external_node_modules="$(readlink -f -- "${node_modules_dir}")"
  if [ ! -d "${external_node_modules}" ]; then
    return 0
  fi
  if [ -e "${node_modules_restore_path}" ]; then
    echo "refusing to reuse stale node_modules restore path: ${node_modules_restore_path}" >&2
    return 1
  fi
  mv -- "${node_modules_dir}" "${node_modules_restore_path}"
  mkdir -- "${node_modules_dir}"
  node_modules_overlay=1
  local entry name
  while IFS= read -r -d '' entry; do
    name="${entry##*/}"
    if [ "${name}" = ".vite-temp" ]; then
      continue
    fi
    ln -s -- "${external_node_modules}/${name}" "${node_modules_dir}/${name}"
  done < <(find "${external_node_modules}" -mindepth 1 -maxdepth 1 -print0)
  mkdir -- "${node_modules_dir}/.vite-temp"
}

on_exit() {
  local status=$?
  restore_node_modules || true
  return "${status}"
}

on_signal() {
  restore_node_modules || true
  exit 143
}

trap on_exit EXIT
trap on_signal HUP INT TERM
prepare_node_modules_overlay || exit $?

# Keep the small process-resource receipt on the project filesystem.  The
# append-only runtime may be operator-mounted on NTFS for legacy reasons,
# but the ntfs3 driver has already faulted while bash performed this tiny
# atomic write.  The receipt is diagnostic build metadata, not prediction
# data, so it must not be allowed to take down the whole post-chain.
resource_dir="${MATCHLINE_SITES_BUILD_RESOURCE_DIR:-${project_dir}/.runtime}"
time_file="${resource_dir}/sites-build.time-v"
resource_file="${resource_dir}/sites-build-resource.json"
tmp_resource_file="${resource_file}.tmp.$$"

mkdir -p "${resource_dir}"
rm -f "${time_file}"

started_at="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"
/usr/bin/time -v -o "${time_file}" /usr/bin/npm --prefix "${project_dir}" run build
status=$?
finished_at="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"

# The readmodel writes the compact snapshot before this build.  The raw bundle
# is imported by the server only; it must never be copied into either the
# public source tree or the client asset tree.  Check the compiled Worker
# bundle for the closed static-path guard as part of the same receipt.
if [ "${status}" -eq 0 ]; then
  source_snapshot="${project_dir}/app/offline_snapshot.data.json"
  forbidden_snapshot_name="offline_snapshot.json"
  public_snapshot="${project_dir}/public/${forbidden_snapshot_name}"
  built_client_snapshot="${project_dir}/dist/client/${forbidden_snapshot_name}"
  worker_bundle="${project_dir}/dist/server/index.js"
  route_manifest="${project_dir}/dist/server/.vite/manifest.json"
  if [ ! -f "${source_snapshot}" ] || [ ! -f "${worker_bundle}" ]; then
    echo "Sites build snapshot verification failed: server-only snapshot or Worker bundle is missing" >&2
    status=1
  elif [ -e "${public_snapshot}" ] || [ -L "${public_snapshot}" ] \
    || [ -e "${built_client_snapshot}" ] || [ -L "${built_client_snapshot}" ]; then
    echo "Sites build snapshot verification failed: raw snapshot reached a public/client path" >&2
    status=1
  elif ! grep -Fq "static_snapshot_not_public" "${worker_bundle}"; then
    echo "Sites build snapshot verification failed: compiled Worker static-path guard is missing" >&2
    status=1
  elif [ ! -f "${route_manifest}" ]; then
    echo "Sites build snapshot verification failed: server route manifest is missing" >&2
    status=1
  elif ! ROUTE_MANIFEST="${route_manifest}" /usr/bin/node --input-type=module <<'NODE'
import { readFile } from "node:fs/promises";

const manifestPath = process.env.ROUTE_MANIFEST;
if (!manifestPath) process.exit(2);
const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
const route = manifest["app/api/matches/route.ts"];
if (!route || typeof route !== "object") {
  console.error("matches route is absent from the Vite server manifest");
  process.exit(1);
}
const dependencies = [
  ...(Array.isArray(route.imports) ? route.imports : []),
  ...(Array.isArray(route.dynamicImports) ? route.dynamicImports : []),
];
if (dependencies.some((entry) => typeof entry === "string" && entry.includes("offline-snapshot"))) {
  console.error("matches route still depends on the bundled offline snapshot chunk");
  process.exit(1);
}
NODE
  then
    echo "Sites build snapshot verification failed: matches route still bundles the offline snapshot" >&2
    status=1
  fi
  if [ "${status}" -ne 0 ]; then
    if [ -f "${source_snapshot}" ]; then
      echo "source_sha256=$(sha256sum "${source_snapshot}" | awk '{print $1}')" >&2
    fi
    if [ -f "${worker_bundle}" ]; then
      echo "worker_sha256=$(sha256sum "${worker_bundle}" | awk '{print $1}')" >&2
    fi
  fi
fi

max_rss_kb="$(awk -F: '/Maximum resident set size \(kbytes\)/ {gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2); print $2; exit}' "${time_file}" 2>/dev/null)"
swap_count="$(awk -F: '/Swaps:/ {gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2); print $2; exit}' "${time_file}" 2>/dev/null)"
elapsed="$(awk '/Elapsed \(wall clock\) time/ {sub(/^.*\):[[:space:]]*/, ""); gsub(/^[[:space:]]+|[[:space:]]+$/, ""); print; exit}' "${time_file}" 2>/dev/null)"

max_rss_kb="${max_rss_kb:-null}"
swap_count="${swap_count:-null}"
elapsed="${elapsed:-null}"

{
  printf '{\n'
  printf '  "schema_version": "matchline.sites_build_resource.v1",\n'
  printf '  "scope": "vinext_build_process_only",\n'
  printf '  "started_at": "%s",\n' "${started_at}"
  printf '  "finished_at": "%s",\n' "${finished_at}"
  printf '  "exit_status": %s,\n' "${status}"
  printf '  "maximum_resident_set_size_kb": %s,\n' "${max_rss_kb}"
  printf '  "swaps": %s,\n' "${swap_count}"
  printf '  "elapsed_wall_clock": "%s",\n' "${elapsed}"
  printf '  "raw_time_file": "%s"\n' "${time_file}"
  printf '}\n'
} > "${tmp_resource_file}"
mv -f "${tmp_resource_file}" "${resource_file}"

exit "${status}"
