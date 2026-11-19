# shellcheck shell=bash
# Sourced by the apt-install action's steps, and by its tests: apt's two calls, each bounded and
# retried.
#
# A mirror fails in three ways, each seen in this organisation's CI: it refuses a download for a
# moment (a 503), which apt's own Acquire::Retries covers within one call; it hangs, which only a
# timeout ends, since nothing else bounds a call inside a composite action; and its index names a
# package version the pool does not hold yet (a 404 on arm64's ports mirror), which no retry of
# the download fixes, only reading the index again once the pool has caught up.  So each call
# has a timeout and apt's own retries, a failed install reads the index again, and the whole is
# tried APT_ATTEMPTS times, APT_DELAY seconds apart.  The job's own timeout-minutes stays the
# backstop over all of it.

APT_ATTEMPTS=5
APT_DELAY=15
APT_UPDATE_SECONDS=300
APT_INSTALL_SECONDS=900

# Reads the package lists, once, bounded.
apt_update_once() {
  sudo timeout "$APT_UPDATE_SECONDS" apt-get -o Acquire::Retries=3 update
}

# Installs the packages named, once, bounded; after a failure, reads the package lists again for
# the next attempt, since a stale index is one cause a retry of the download cannot fix.
apt_install_once() {
  local status=0
  sudo timeout "$APT_INSTALL_SECONDS" apt-get -o Acquire::Retries=3 install -y \
    --no-install-recommends "$@" || status=$?
  if ((status != 0)); then
    apt_update_once || true
  fi
  return "$status"
}

# Runs the command after WHAT until it succeeds, APT_ATTEMPTS times at most, and fails with the
# last attempt's status, saying what was being done.  Exit status 124 is a timeout.
apt_retry() {
  local what=$1
  shift
  local attempt=1 status
  while true; do
    status=0
    "$@" || status=$?
    if ((status == 0)); then
      return 0
    fi
    if ((attempt >= APT_ATTEMPTS)); then
      echo "::error::apt-install: $what failed on all $APT_ATTEMPTS attempts; the last exited $status$( ((status == 124)) && echo ', a timeout' ), and apt's output above says why" >&2
      return "$status"
    fi
    echo "::warning::apt-install: $what failed with exit $status$( ((status == 124)) && echo ', a timeout,' ) on attempt $attempt of $APT_ATTEMPTS; trying again in ${APT_DELAY}s"
    sleep "$APT_DELAY"
    attempt=$((attempt + 1))
  done
}
