#!/usr/bin/env bash

# Bootstrap a multi-node Talos cluster
# For a 3-node control plane:
# 1. Apply config to all nodes
# 2. Bootstrap etcd on the first control plane
# 3. Wait for cluster to be ready
# 4. Fetch kubeconfig
# 5. Apply resources

function apply_node_config_bootstrap() {
    local node_name="$1"
    local node_ip
    local schematic_file
    local node_config
    local base_config

    node_ip=$(get_node_ip "$node_name")
    schematic_file=$(get_node_schematic "$node_name")
    node_config="${TALOS_DIR}/nodes/${node_name}.yaml"
    base_config="${TALOS_DIR}/machineconfig.yaml"

    if [[ ! -f "$node_config" ]]; then
        gum log --structured --level error "Node config not found" "node" "$node_name" "file" "$node_config"
        return 1
    fi

    gum log --structured --level info "Applying config" "node" "$node_name" "ip" "$node_ip"

    # Generate schematic ID for this node
    local schematic_id
    schematic_id=$(generate_schematic "$schematic_file")
    gum log --structured --level debug "Schematic ID" "id" "$schematic_id"

    export TALOS_SCHEMATIC="$schematic_id"

    # Create temp file for processed node config
    local tmp_node_config
    tmp_node_config=$(mktemp)
    trap 'rm -f "${tmp_node_config}"' RETURN

    # Process node config through envsubst
    envsubst < "${node_config}" > "${tmp_node_config}"

    # Render base config, merge with node config, and apply (insecure for initial bootstrap)
    if ! op inject -i "${base_config}" | envsubst | \
         talosctl machineconfig patch /dev/stdin --patch "@${tmp_node_config}" | \
         talosctl --nodes "${node_ip}" apply-config --insecure --file /dev/stdin --config-patch "@${TALOS_DIR}/patches/patches.yaml" 2>&1; then
        gum log --structured --level error "Failed to apply config" "node" "$node_name"
        return 1
    fi

    gum log --structured --level info "Config applied" "node" "$node_name"
    return 0
}

function apply_talos_configs() {
    gum log --structured --level info "Applying Talos configuration to all nodes"

    local nodes
    nodes=$(get_node_names)
    local failed=0

    for node in $nodes; do
        if ! apply_node_config_bootstrap "$node"; then
            ((failed++))
        fi
    done

    if [[ $failed -gt 0 ]]; then
        gum log --structured --level error "Some nodes failed" "failed" "$failed"
        exit 1
    fi

    gum log --structured --level info "All node configs applied successfully"
}

function bootstrap_talos() {
    local first_node
    first_node=$(get_node_names | head -1)
    local node_ip
    node_ip=$(get_node_ip "$first_node")

    gum log --structured --level info "Bootstrapping etcd" "node" "$first_node" "ip" "$node_ip"

    local output
    local retries=0
    local max_retries=30

    while [[ $retries -lt $max_retries ]]; do
        output=$(talosctl --nodes "$node_ip" bootstrap 2>&1 || true)

        if [[ "${output}" == *"AlreadyExists"* ]]; then
            gum log --structured --level info "Cluster already bootstrapped"
            return 0
        fi

        if [[ "${output}" == *"connection refused"* ]] || [[ "${output}" == *"deadline exceeded"* ]]; then
            ((retries++))
            gum log --structured --level info "Waiting for node to be ready" "attempt" "$retries/$max_retries"
            sleep 10
            continue
        fi

        # Bootstrap succeeded
        gum log --structured --level info "Etcd bootstrapped successfully"
        return 0
    done

    gum log --structured --level error "Failed to bootstrap after $max_retries attempts"
    exit 1
}

function wait_for_nodes() {
    gum log --structured --level info "Waiting for all nodes to be ready"

    local expected_nodes
    expected_nodes=$(get_node_names | wc -l | tr -d ' ')

    local retries=0
    local max_retries=60

    while [[ $retries -lt $max_retries ]]; do
        local ready_nodes
        ready_nodes=$(kubectl get nodes --no-headers 2>/dev/null | grep -c " Ready" || echo "0")

        if [[ "$ready_nodes" -ge "$expected_nodes" ]]; then
            gum log --structured --level info "All nodes ready" "count" "$ready_nodes"
            return 0
        fi

        ((retries++))
        gum log --structured --level info "Waiting for nodes" "ready" "$ready_nodes/$expected_nodes" "attempt" "$retries/$max_retries"
        sleep 10
    done

    gum log --structured --level warn "Timeout waiting for all nodes, continuing anyway"
}

function apply_configs() {
    local config_dir="${COMPONENTS_DIR}/common/vars"
    gum log --structured --level info "Applying configs"
    if [[ ! -d "$config_dir" ]]; then
        gum log --structured --level error "Config directory not found" "directory" "$config_dir"
        exit 1
    fi
    if ! kubectl apply --filename "${config_dir}/cluster-settings.yaml" &>/dev/null; then
        gum log --structured --level error "Failed to apply cluster settings"
        exit 1
    fi
    if ! sops --decrypt "${config_dir}/cluster-secrets.secret.sops.yaml" |
        kubectl apply --filename - &>/dev/null; then
        gum log --structured --level error "Failed to apply cluster secrets"
        exit 1
    fi
    gum log --structured --level info "Configs applied successfully"
}

function apply_resources() {
    local resources_file="${BOOTSTRAP_DIR}/resources.yaml.j2"
    gum log --structured --level info "Applying resources"
    if [[ ! -f "$resources_file" ]]; then
        gum log --structured --level error "Resources file not found" "file" "$resources_file"
        exit 1
    fi
    local output
    output=$(render_template "$resources_file")
    if echo "$output" | kubectl diff --filename - &>/dev/null; then
        gum log --structured --level warn "Resources are up-to-date"
        return 0
    fi
    if echo "$output" | kubectl apply --server-side --filename - &>/dev/null; then
        gum log --structured --level info "Resources applied"
    else
        gum log --structured --level error "Failed to apply resources"
        exit 1
    fi
}

function apply_crds() {
    gum log --structured --level info "Applying CRDs"
    local -r crds=(
        # renovate: datasource=github-releases depName=kubernetes-sigs/gateway-api
        https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.4.1/experimental-install.yaml
        # renovate: datasource=github-releases depName=prometheus-operator/prometheus-operator
        https://github.com/prometheus-operator/prometheus-operator/releases/download/v0.87.0/stripped-down-crds.yaml
        # renovate: datasource=github-releases depName=kubernetes-sigs/external-dns
        https://raw.githubusercontent.com/kubernetes-sigs/external-dns/refs/tags/v0.20.0/config/crd/standard/dnsendpoints.externaldns.k8s.io.yaml
    )
    for crd in "${crds[@]}"; do
        if kubectl diff --filename "${crd}" &>/dev/null; then
            gum log --structured --level info "CRDs are up-to-date" "crd" "$crd"
            continue
        fi
        if kubectl apply --server-side --filename "${crd}" &>/dev/null; then
            gum log --structured --level info "CRDs applied" "crd" "$crd"
        else
            gum log --structured --level error "Failed to apply CRD" "crd" "$crd"
        fi
    done
}

function apply_helm_releases() {
    local helmfile_file="${BOOTSTRAP_DIR}/helmfile.yaml"
    gum log --structured --level info "Applying Helm releases"
    if [[ ! -f "$helmfile_file" ]]; then
        gum log --structured --level error "Helmfile not found" "file" "$helmfile_file"
        exit 1
    fi
    if ! helmfile --file "${helmfile_file}" sync --hide-notes; then
        gum log --structured --level error "Failed to apply Helm releases"
        exit 1
    fi
    gum log --structured --level info "Helm releases applied successfully"
}

function show_cluster_info() {
    echo ""
    gum style --bold "Cluster Information"
    echo ""

    local nodes
    nodes=$(get_node_names)

    echo "Nodes:"
    for node in $nodes; do
        local ip
        ip=$(get_node_ip "$node")
        printf "  - %-20s %s\n" "$node" "$ip"
    done

    echo ""
    echo "Control Plane VIP: $(get_cluster_vip)"
    echo ""
}

function main() {
    check_env KUBECONFIG KUBERNETES_VERSION TALOS_VERSION COMPONENTS_DIR NODES_FILE
    check_cli helmfile jq kubectl kustomize minijinja-cli op talosctl yq envsubst

    show_cluster_info

    gum confirm "Bootstrap the Talos cluster with the above nodes ... continue?" || exit 0

    op_signin
    apply_talos_configs
    bootstrap_talos
    fetch_kubeconfig "$(get_first_node_ip)"
    wait_for_nodes
    apply_resources
    apply_configs
    apply_crds
    apply_helm_releases

    gum log --structured --level info "Cluster bootstrapped successfully!"

    echo ""
    gum style --bold --foreground 2 "✓ Cluster is ready!"
    echo ""
    kubectl get nodes -o wide
}

main
