#!/bin/sh
# Replaces preview-pipeline files with ICC-profile-preserving versions
# (upstream: https://github.com/nextcloud/server/issues/22951).
# Runs as a before-starting hook, after the entrypoint has synced
# /usr/src/nextcloud to /var/www/html. Files are only replaced when their
# checksum matches the pristine 33.0.5 release; on mismatch (Nextcloud
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

apply lib/private/Image.php 2b6e7df70f411b5ff77d18ac8f78c5a8ee04d917736911b422fc62e9dc297b53 Image.php
apply lib/private/Preview/HEIC.php 724fb54b90fcc04bd0764c7841ccb6a2f58ba0104abdfb9aa3c5e0803ed6804a HEIC.php

exit 0
