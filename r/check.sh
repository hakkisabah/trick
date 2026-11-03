#!/bin/sh
# Build and check the R package the way CRAN will.
#
#   sh r/check.sh            # R CMD build + R CMD check --as-cran
#
# One host quirk is handled here rather than in the package: some builds of R
# hardcode the path of `which` (conda-forge patched R looks for /usr/bin/which),
# and a machine without that file cannot load even `utils`. When it is missing,
# the check runs inside an unprivileged user namespace that overlays a `which`
# shim onto /usr/bin for this process tree only - no root, no change to the host.
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PKG=$REPO/r/trick
R_BIN=${R_BIN:-R}

command -v "$R_BIN" >/dev/null 2>&1 || {
  echo "R is not on PATH; set R_BIN to the R binary" >&2
  exit 1
}

run_check() {
  # `--as-cran` asks CRAN for its incoming-feasibility data over HTTPS. A conda
  # build of R ships a libcurl without the system trust store, so the request
  # fails with "SSL connect error" and the check halts before testing anything.
  # Pointing it at the host bundle is the difference between a check that runs
  # and one that cannot start.
  if [ -z "${CURL_CA_BUNDLE:-}" ] && [ -f /etc/ssl/certs/ca-certificates.crt ]; then
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
    export CURL_CA_BUNDLE SSL_CERT_FILE
  fi
  work=$(mktemp -d)
  cd "$work"
  "$R_BIN" CMD build "$PKG" >build.log 2>&1 || { cat build.log; exit 1; }
  tarball=$(ls trick_*.tar.gz | head -1)
  echo "built $tarball"
  "$R_BIN" CMD check --as-cran --no-manual "$tarball"
  echo "check directory: $work/${tarball%.tar.gz}.Rcheck"
}

if [ -x /usr/bin/which ]; then
  run_check
  exit 0
fi

echo "no /usr/bin/which on this host; running the check in a user namespace" >&2
shim_dir=$(mktemp -d)
cat >"$shim_dir/which" <<'SHIM'
#!/bin/sh
for name in "$@"; do
  case $name in -*) continue ;; esac
  found=$(command -v "$name" 2>/dev/null || true)
  [ -n "$found" ] && printf '%s\n' "$found"
done
SHIM
chmod +x "$shim_dir/which"

exec unshare -rm sh -c '
set -e
mkdir -p /tmp/ubin.$$
mount -t tmpfs tmpfs /tmp/ubin.$$
cp -a /usr/bin/. /tmp/ubin.$$/ 2>/dev/null || true
cp '"$shim_dir"'/which /tmp/ubin.$$/which
chmod +x /tmp/ubin.$$/which
mount --bind /tmp/ubin.$$ /usr/bin
R_BIN='"$R_BIN"' sh '"$0"'
'
