#!/bin/sh
# Replaces preview-pipeline files with ICC-profile-preserving versions
# (upstream: https://github.com/nextcloud/server/issues/22951).
# Runs as a before-starting hook, after the entrypoint has synced
# /usr/src/nextcloud to /var/www/html. Files are only replaced when their
# checksum matches the pristine 34.0.1 release; on mismatch (Nextcloud
# upgraded) the hook warns and leaves the code untouched — rebase the files
# in kubernetes/apps/default/nextcloud/app/resources/icc-patch/ against the
# new release and update the checksums below.
PATCH_DIR=/opt/icc-patch

apply() {
    target="/var/www/html/$1"
    pristine_sha="$2"
    source="$PATCH_DIR/$3"

    current_sha=$(sha256sum "$target" | cut -d' ' -f1)
    patched_sha=$(sha256sum "$source" | cut -d' ' -f1)

    if [ "$current_sha" = "$patched_sha" ]; then
        echo "icc-patch: $1 already patched"
    elif [ "$current_sha" = "$pristine_sha" ]; then
        cp "$source" "$target"
        echo "icc-patch: patched $1"
    else
        echo "icc-patch: WARNING: $1 has unexpected checksum $current_sha, skipping"
    fi
}

apply lib/private/Image.php 6c1ccdfc286d7980a4f6c2131dc3b168463a875a46dc7d7b5afed02777e35cb1 Image.php
apply lib/private/Preview/HEIC.php 2f8fc631afc36fc321fcca030668f9c526cf91f3a6a0bd988ea3a94dcb20cbd2 HEIC.php

exit 0
