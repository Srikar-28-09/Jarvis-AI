#!/usr/bin/env bash
# Build patched wheels for Real-ESRGAN's unmaintained dependencies.
#
# basicsr / gfpgan / facexlib (xinntao, last released 2022) read their version
# in setup.py with:
#
#     exec(compile(f.read(), version_file, 'exec'))
#     return locals()['__version__']
#
# Python 3.13+ implements PEP 667: locals() inside a function returns an
# independent snapshot that exec() can no longer mutate, so the read raises
# `KeyError: '__version__'` and the sdist build fails. That is why the Cookbook
# "install realesrgan" button dies on the python:3.14 image. The packages have
# no fixed release, so we patch get_version() to exec into an explicit namespace
# dict (works on every Python) and build wheels from the patched source.
#
# Usage: build-realesrgan-wheels.sh [OUTPUT_DIR]   (default: /wheels)
set -euo pipefail

OUT="${1:-/wheels}"
mkdir -p "$OUT"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"
mkdir -p .pypi-json

# Pinned to the versions Real-ESRGAN 0.3.0 resolves to.
SPECS="basicsr==1.4.2 gfpgan==1.3.8 facexlib==0.3.0"

CURL_RETRY_COUNT="${CURL_RETRY_COUNT:-5}"
CURL_CONNECT_TIMEOUT="${CURL_CONNECT_TIMEOUT:-15}"
CURL_API_MAX_TIME="${CURL_API_MAX_TIME:-120}"
CURL_ARTIFACT_MAX_TIME="${CURL_ARTIFACT_MAX_TIME:-600}"
CURL_RETRY_MAX_TIME="${CURL_RETRY_MAX_TIME:-900}"

download_with_retry() {
  local label="$1"
  shift
  echo ">> downloading ${label}" >&2
  curl --fail --silent --show-error --location \
    --retry "$CURL_RETRY_COUNT" \
    --retry-all-errors \
    --retry-delay 2 \
    --retry-max-time "$CURL_RETRY_MAX_TIME" \
    --connect-timeout "$CURL_CONNECT_TIMEOUT" \
    --max-time "$CURL_ARTIFACT_MAX_TIME" \
    "$@" \
    2>&1
}

for spec in $SPECS; do
  name="${spec%%==*}"
  ver="${spec##*==}"
  # pip download builds metadata (and trips the same bug), so fetch the raw
  # sdist URL from the PyPI JSON API instead.
  json_file=".pypi-json/${name}-${ver}.json"
  echo ">> resolving ${name} ${ver} from PyPI"
  curl --fail --silent --show-error --location \
    --retry "$CURL_RETRY_COUNT" \
    --retry-all-errors \
    --retry-delay 2 \
    --retry-max-time "$CURL_RETRY_MAX_TIME" \
    --connect-timeout "$CURL_CONNECT_TIMEOUT" \
    --max-time "$CURL_API_MAX_TIME" \
    "https://pypi.org/pypi/${name}/${ver}/json" \
    --output "$json_file"

  resolved="$(
    python - "$name" "$ver" "$json_file" <<'PY'
import json, re, sys

name, ver, json_file = sys.argv[1:]
with open(json_file, "r", encoding="utf-8") as handle:
    data = json.load(handle)

for artifact in data.get("urls", []):
    if artifact.get("packagetype") != "sdist":
        continue
    url = artifact.get("url")
    sha256 = (artifact.get("digests") or {}).get("sha256")
    if not isinstance(url, str) or not url.startswith("https://"):
        continue
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        continue
    print(url)
    print(sha256.lower())
    break
else:
    sys.exit(f"no verified sdist found for {name}=={ver}")
PY
  )"
  url="$(printf '%s\n' "$resolved" | sed -n '1p')"
  expected_sha256="$(printf '%s\n' "$resolved" | sed -n '2p')"
  if [[ -z "$url" || -z "$expected_sha256" ]]; then
    echo "ERROR: PyPI resolution returned incomplete artifact metadata for ${name}==${ver}" >&2
    exit 1
  fi

  archive="${name}.tar.gz"
  partial="${archive}.part"
  rm -f "$partial"
  echo ">> fetching ${name} ${ver}: ${url}"
  if ! download_with_retry "${name} ${ver} sdist" --output "$partial" "$url"; then
    echo "ERROR: failed to download ${name}==${ver} sdist after ${CURL_RETRY_COUNT} retries" >&2
    echo "       URL: ${url}" >&2
    rm -f "$partial"
    exit 35
  fi
  if ! printf '%s  %s\n' "$expected_sha256" "$partial" | sha256sum --check --strict -; then
    echo "ERROR: SHA-256 verification failed for ${name}==${ver} sdist" >&2
    echo "       Expected: ${expected_sha256}" >&2
    echo "       URL: ${url}" >&2
    rm -f "$partial"
    exit 36
  fi
  mv -f "$partial" "$archive"
  tar xzf "${name}.tar.gz"
done

echo ">> patching get_version()"
python - <<'PY'
import pathlib
old_exec = "exec(compile(f.read(), version_file, 'exec'))"
new_exec = "_ver_ns = {}\n        exec(compile(f.read(), version_file, 'exec'), _ver_ns)"
old_ret = "return locals()['__version__']"
new_ret = "return _ver_ns['__version__']"
patched = 0
for setup in pathlib.Path(".").glob("*/setup.py"):
    s = setup.read_text()
    if old_exec in s and old_ret in s:
        setup.write_text(s.replace(old_exec, new_exec).replace(old_ret, new_ret))
        print("   patched", setup)
        patched += 1
assert patched == 3, f"expected to patch 3 setup.py files, patched {patched}"
PY

echo ">> building wheels into ${OUT}"
pip wheel --no-deps -w "$OUT" ./basicsr-* ./gfpgan-* ./facexlib-*
ls -l "$OUT"
