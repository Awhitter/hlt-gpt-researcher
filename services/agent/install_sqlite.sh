#!/bin/sh
# Build only in the target-platform Docker stage, from the official stable
# amalgamation. Verify before extraction; no distro-unstable repository needed.
set -eu

sqlite_archive=/tmp/sqlite-autoconf-3530400.tar.gz
curl --fail --show-error --location --max-time 90 \
    https://www.sqlite.org/2026/sqlite-autoconf-3530400.tar.gz \
    --output "$sqlite_archive"
python - "$sqlite_archive" <<'PY'
import hashlib
import sys
from pathlib import Path

archive = Path(sys.argv[1]).read_bytes()
expected = "454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338"
assert hashlib.sha3_256(archive).hexdigest() == expected, "SQLite source checksum mismatch"
PY
tar -xzf "$sqlite_archive" -C /tmp
cd /tmp/sqlite-autoconf-3530400
CFLAGS="-Os -DSQLITE_ENABLE_COLUMN_METADATA -DSQLITE_ENABLE_UNLOCK_NOTIFY -DSQLITE_SECURE_DELETE -DSQLITE_MAX_VARIABLE_NUMBER=250000" \
    ./configure --prefix=/usr/local --disable-static --disable-readline \
    --fts4 --fts5 --rtree --session --dbstat
make -j2 libsqlite3.so
make install-dll install-headers
ldconfig
python -c 'import sqlite3; assert sqlite3.sqlite_version == "3.53.4", sqlite3.sqlite_version'
rm -rf /tmp/sqlite-autoconf-3530400 "$sqlite_archive"
