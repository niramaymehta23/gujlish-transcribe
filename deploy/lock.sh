#!/usr/bin/env bash
# Pin every Python package to an exact version, so a rebuild installs what was tested instead of whatever is newest.
#   requirements.lock       what the Docker image installs (Linux, Python 3.12): requirements.txt and all it pulls in
#   requirements-test.lock  what the CI checks install (Python 3.10 to 3.14): the image's versions wherever they
#                           install, the newest that do on older Pythons
# Run it after changing requirements.txt or requirements-test.txt: packages already pinned keep their versions.
# To upgrade on purpose, run it with --upgrade (everything) or --upgrade-package NAME, then run the tests; the next
# deploy builds from the new pins. Needs uv: https://docs.astral.sh/uv/
set -euo pipefail
cd "$(dirname "$0")/.."

uv pip compile requirements.txt --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
  --custom-compile-command "deploy/lock.sh" --quiet "$@" -o requirements.lock

# The CI lock follows the image's pins on Python 3.12 and later; on older Pythons a pin may not install (numpy).
image_pins=$(mktemp)
trap 'rm -f "$image_pins"' EXIT
sed -n 's/^\([A-Za-z0-9._-]*==[^ ;]*\).*/\1 ; python_version >= "3.12"/p' requirements.lock > "$image_pins"
uv pip compile requirements-test.txt --universal --python-version 3.10 --constraint "$image_pins" \
  --custom-compile-command "deploy/lock.sh" --no-annotate --quiet "$@" -o requirements-test.lock
echo "Pinned: requirements.lock, requirements-test.lock"
