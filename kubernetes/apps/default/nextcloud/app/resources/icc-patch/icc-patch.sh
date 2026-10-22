#!/bin/sh
# Replaces preview-pipeline files with ICC-profile-preserving versions
# (upstream: https://github.com/nextcloud/server/issues/22951).
# Runs as a before-starting hook, after the entrypoint has synced
# /usr/src/nextcloud to /var/www/html. Files are only replaced when their
# checksum matches the pristine 35.0.2 release; on mismatch (Nextcloud
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

apply lib/private/Image.php d13dca4dbdf971bc295326e61e198ecfbb46ded7bf12ae805bd9c80d4cd4b87e Image.php
apply lib/private/Preview/HEIC.php 8487c7538028bda35f668e46bb0cbbe8d58ca0e2cbbd82bc6c9d0c274d26c942 HEIC.php

exit 0
