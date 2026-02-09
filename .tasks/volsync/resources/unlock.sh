#!/usr/bin/env bash

NFS_SERVER="192.168.10.98"
NFS_PATH="/mnt/rust/volsync"
RESTIC_IMAGE="docker.io/restic/restic:latest"

# --- Unlock local (NFS) repositories ---

gum log --structured --level info "=== Unlocking local (NFS) repositories ==="

mapfile -t local_secrets < <(kubectl get secrets --all-namespaces --no-headers -o custom-columns="NAMESPACE:.metadata.namespace,NAME:.metadata.name" | grep "volsync-local-secret$")

if [ -z "${local_secrets[*]}" ]; then
    gum log --structured --level warn "No local volsync secrets found"
else
    for secret in "${local_secrets[@]}"; do
        namespace=$(echo "$secret" | awk '{print $1}')
        secret_name=$(echo "$secret" | awk '{print $2}')
        application=$(echo "$secret_name" | sed -E 's|-volsync-local-secret$||')

        secret_data=$(kubectl --namespace "$namespace" get secret "$secret_name" -o jsonpath='{.data}')
        RESTIC_PASSWORD=$(echo "$secret_data" | jq -r '.RESTIC_PASSWORD' | base64 -d)
        RESTIC_REPOSITORY=$(echo "$secret_data" | jq -r '.RESTIC_REPOSITORY' | base64 -d)

        if [ -z "$RESTIC_PASSWORD" ] || [ -z "$RESTIC_REPOSITORY" ]; then
            gum log --structured --level error "Invalid secret data for $namespace/$secret_name, skipping..."
            continue
        fi

        gum log --structured --level info "Processing local secret: $namespace/$secret_name"

        output=$(kubectl run "volsync-unlock-${application}" \
            --namespace "$namespace" \
            --rm -i --restart=Never \
            --image="$RESTIC_IMAGE" \
            --overrides="{
                \"spec\": {
                    \"volumes\": [{
                        \"name\": \"repository\",
                        \"nfs\": {
                            \"server\": \"$NFS_SERVER\",
                            \"path\": \"$NFS_PATH\"
                        }
                    }],
                    \"containers\": [{
                        \"name\": \"restic-unlock\",
                        \"image\": \"$RESTIC_IMAGE\",
                        \"command\": [\"restic\", \"unlock\", \"--remove-all\"],
                        \"env\": [
                            {\"name\": \"RESTIC_REPOSITORY\", \"value\": \"$RESTIC_REPOSITORY\"},
                            {\"name\": \"RESTIC_PASSWORD\", \"value\": \"$RESTIC_PASSWORD\"}
                        ],
                        \"volumeMounts\": [{
                            \"name\": \"repository\",
                            \"mountPath\": \"/repository\"
                        }]
                    }]
                }
            }" 2>&1)

        if echo "$output" | grep -q "successfully removed"; then
            gum log --structured --level info "Removed locks for $application" "output" "$(echo "$output" | grep "successfully removed")"
        else
            gum log --structured --level info "No lock files found for $application"
        fi
    done
fi

# --- Unlock remote (S3/R2) repositories ---

gum log --structured --level info "=== Unlocking remote (S3) repositories ==="

mapfile -t secrets < <(kubectl get secrets --all-namespaces --no-headers -o custom-columns="NAMESPACE:.metadata.namespace,NAME:.metadata.name" | grep "remote-secret$")

if [ -z "${secrets[*]}" ]; then
    gum log --structured --level warn "No remote volsync secrets found"
fi

for secret in "${secrets[@]}"; do
    namespace=$(echo "$secret" | awk '{print $1}')
    secret_name=$(echo "$secret" | awk '{print $2}')
    gum log --structured --level info "Processing secret: $namespace/$secret_name"

    secret_data=$(kubectl --namespace "$namespace" get secret "$secret_name" -o jsonpath='{.data}')

    # INFO: Requires explicit exportation, aws cli querk
    export AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID RESTIC_PASSWORD RESTIC_REPOSITORY
    AWS_ACCESS_KEY_ID=$(echo "$secret_data" | jq -r '.AWS_ACCESS_KEY_ID' | base64 -d)
    AWS_SECRET_ACCESS_KEY=$(echo "$secret_data" | jq -r '.AWS_SECRET_ACCESS_KEY' | base64 -d)
    RESTIC_PASSWORD=$(echo "$secret_data" | jq -r '.RESTIC_PASSWORD' | base64 -d)
    RESTIC_REPOSITORY=$(echo "$secret_data" | jq -r '.RESTIC_REPOSITORY' | base64 -d)

    bucket=$(echo "$RESTIC_REPOSITORY" | sed -E 's|s3:https://[^/]+/([^/]+)/.*|\1|')
    application=$(echo "$RESTIC_REPOSITORY" | sed -E 's|s3:https://[^/]+/[^/]+/(.*)|\1|')
    r2_endpoint="$(echo "$RESTIC_REPOSITORY" | sed -E 's|s3:(https://[^/]+).*|\1|')"

    # s3:<$r2_endpoint>/<$bucket>/<$application>
    if [ -z "$AWS_ACCESS_KEY_ID" ] || [ -z "$AWS_SECRET_ACCESS_KEY" ] ||
        [ -z "$RESTIC_PASSWORD" ] || [ -z "$RESTIC_REPOSITORY" ] ||
        [ -z "$bucket" ] || [ -z "$application" ] || [ -z "$r2_endpoint" ]; then
        gum log --structured --level error "Error: Invalid data found"
        gum log --structured --level error "AWS_ACCESS_KEY_ID: ${AWS_ACCESS_KEY_ID:-<empty>}"
        gum log --structured --level error "AWS_SECRET_ACCESS_KEY: ${AWS_SECRET_ACCESS_KEY:-<empty>}"
        gum log --structured --level error "RESTIC_PASSWORD: ${RESTIC_PASSWORD:-<empty>}"
        gum log --structured --level error "RESTIC_REPOSITORY: ${RESTIC_REPOSITORY:-<empty>}"
        gum log --structured --level error "Bucket: ${bucket:-<empty>}"
        gum log --structured --level error "Application: ${application:-<empty>}"
        gum log --structured --level error "R2 Endpoint: ${r2_endpoint:-<empty>}"
        gum log --structured --level warn "Skipping this application..."
        continue
    fi

    if aws s3 ls --endpoint-url "$r2_endpoint" "s3://$bucket/$application/locks" >/dev/null 2>&1; then
        gum log --structured --level info "Found lock files for $application, removing them..."
        aws s3 rm --endpoint-url "$r2_endpoint" "s3://$bucket/$application/locks" --recursive
    else
        gum log --structured --level info "No lock files found for $application"
    fi
done
