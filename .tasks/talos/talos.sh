#!/usr/bin/env bash

OPTIONS=("Apply Config" "Apply All" "Upgrade Talos" "Upgrade Kubernetes" "Reboot" "Shutdown" "Reset" "Generate Kubeconfig" "Rotate Client Certs" "Node Status" "Help" "Back")

function show_help() {
    echo "Usage: $0 [command] [node] [options]"
    echo ""
    echo "Commands:"
    echo "  apply [node] [-i]  Apply Talos config to a specific node"
    echo "                     Use -i or --insecure for fresh nodes in maintenance mode"
    echo "  apply-all          Apply Talos config to all nodes"
    echo "  upgrade [node]     Upgrade Talos on a specific node"
    echo "  upgrade-all        Upgrade Talos on all nodes (sequential)"
    echo "  upgrade-k8s        Upgrade Kubernetes on the cluster"
    echo "  reboot [node]      Reboot a specific node"
    echo "  shutdown [node]    Shutdown a specific node"
    echo "  reset [node]       Reset a specific node"
    echo "  kubeconfig         Generate the kubeconfig for the cluster"
    echo "  rotate-certs       Rotate Talos client certificate (alias: rotate)"
    echo "  status             Show status of all nodes"
    echo ""
    echo "Available nodes:"
    get_node_names | while read -r node; do
        local ip
        ip=$(get_node_ip "$node")
        echo "  - $node ($ip)"
    done
    echo ""
    echo "Options:"
    echo "  -h, --help, help    Display this help message"
    exit 0
}

function menu() {
    choice=$(gum choose "${OPTIONS[@]}")
    if [ -n "$choice" ]; then
        echo "$choice"
    fi
}

# Render and apply config for a single node
# Usage: apply_node_config <node_name> [--insecure]
function apply_node_config() {
    local node_name="$1"
    local insecure_flag=""

    # Check for --insecure flag
    if [[ "$2" == "--insecure" ]] || [[ "$2" == "-i" ]]; then
        insecure_flag="--insecure"
        gum log --structured --level info "Using insecure mode for fresh node"
    fi

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
    gum log --structured --level info "Schematic ID generated" "id" "$schematic_id"

    # Set environment variables for envsubst
    export TALOS_SCHEMATIC="$schematic_id"

    # Create temp file for processed node config
    local tmp_node_config
    tmp_node_config=$(mktemp)
    trap 'rm -f "${tmp_node_config}"' RETURN

    # Process node config through envsubst
    envsubst < "${node_config}" > "${tmp_node_config}"

    # Render base config, merge with node config, and apply
    # Use --insecure for fresh nodes in maintenance mode
    if ! op inject -i "${base_config}" | envsubst | \
         talosctl machineconfig patch /dev/stdin --patch "@${tmp_node_config}" | \
         talosctl --nodes "${node_ip}" apply-config --mode auto ${insecure_flag} --file /dev/stdin --config-patch "@${TALOS_DIR}/patches/patches.yaml"; then
        gum log --structured --level error "Failed to apply config" "node" "$node_name"
        return 1
    fi

    gum log --structured --level info "Successfully applied config" "node" "$node_name"
    return 0
}

# Upgrade Talos on a single node
function upgrade_node() {
    local node_name="$1"
    local node_ip
    local schematic_file
    local node_config

    node_ip=$(get_node_ip "$node_name")
    schematic_file=$(get_node_schematic "$node_name")
    node_config="${TALOS_DIR}/nodes/${node_name}.yaml"

    gum log --structured --level info "Upgrading Talos" "node" "$node_name" "ip" "$node_ip"

    # Generate schematic ID for this node
    local schematic_id
    schematic_id=$(generate_schematic "$schematic_file")
    gum log --structured --level info "Schematic ID" "id" "$schematic_id"

    export TALOS_SCHEMATIC="$schematic_id"

    # Get the factory image from the node config (install.image is per-node)
    local factory_image
    if ! factory_image=$(yq --exit-status '.machine.install.image' "${node_config}" 2>/dev/null); then
        gum log --structured --level error "Failed to read install image from node config" "node" "$node_name"
        return 1
    fi

    # Substitute environment variables in the image path
    factory_image=$(echo "$factory_image" | envsubst)

    gum log --structured --level info "Using image" "image" "$factory_image"

    if ! talosctl --nodes "${node_ip}" upgrade --image="${factory_image}" --reboot-mode=powercycle --timeout=10m; then
        gum log --structured --level error "Failed to upgrade Talos" "node" "$node_name"
        return 1
    fi

    gum log --structured --level info "Successfully upgraded Talos" "node" "$node_name"
    return 0
}

function main() {
    local args=("$@")

    if [[ ${#args[@]} -eq 0 || "${args[0]}" == "menu" ]]; then
        args[0]=$(menu)
        if [ -z "${args[0]}" ]; then
            gum log --structured --level error "No choice selected"
            exit 1
        fi
    fi

    case "${args[0]}" in
    "Apply Config" | "apply")
        check_cli op talosctl yq envsubst
        op_signin

        local node="${args[1]}"
        local extra_flag="${args[2]}"

        # Handle: ops talos apply --insecure control-2
        if [[ "$node" == "--insecure" ]] || [[ "$node" == "-i" ]]; then
            extra_flag="--insecure"
            node="${args[2]}"
        fi

        if [[ -z "$node" ]]; then
            node=$(select_node "Select node to apply config")
        fi

        apply_node_config "$node" "$extra_flag"
        ;;

    "Apply All" | "apply-all")
        check_cli op talosctl yq envsubst
        op_signin

        local nodes
        nodes=$(get_node_names)
        local failed=0

        for node in $nodes; do
            if ! apply_node_config "$node"; then
                ((failed++))
            fi
        done

        if [[ $failed -gt 0 ]]; then
            gum log --structured --level error "Some nodes failed" "failed" "$failed"
            exit 1
        fi

        gum log --structured --level info "All nodes configured successfully"
        ;;

    "Upgrade Talos" | "upgrade")
        check_cli talosctl yq op envsubst
        op_signin

        local node="${args[1]}"
        if [[ -z "$node" ]]; then
            node=$(select_node "Select node to upgrade")
        fi

        upgrade_node "$node"
        ;;

    "upgrade-all")
        check_cli talosctl yq op envsubst
        op_signin

        if ! gum confirm "Upgrade Talos on ALL nodes sequentially?"; then
            gum log --structured --level info "Upgrade cancelled"
            exit 0
        fi

        local nodes
        nodes=$(get_node_names)

        for node in $nodes; do
            upgrade_node "$node"
            gum log --structured --level info "Waiting for node to be ready..." "node" "$node"
            sleep 30
        done

        gum log --structured --level info "All nodes upgraded successfully"
        ;;

    "Upgrade Kubernetes" | "upgrade-k8s")
        check_env KUBERNETES_VERSION
        check_cli talosctl

        local node_ip
        node_ip=$(get_first_node_ip)

        gum log --structured --level info "Upgrading Kubernetes to ${KUBERNETES_VERSION}"
        if ! talosctl --nodes "${node_ip}" upgrade-k8s --to "${KUBERNETES_VERSION}"; then
            gum log --structured --level error "Failed to upgrade Kubernetes"
            exit 1
        fi
        gum log --structured --level info "Successfully upgraded Kubernetes"
        ;;

    "Reboot" | "reboot")
        check_cli talosctl

        local node="${args[1]}"
        if [[ -z "$node" ]]; then
            node=$(select_node "Select node to reboot")
        fi

        local node_ip
        node_ip=$(get_node_ip "$node")

        gum log --structured --level info "Rebooting node" "node" "$node" "ip" "$node_ip"
        if ! talosctl --nodes "${node_ip}" reboot --mode=powercycle; then
            gum log --structured --level error "Failed to reboot" "node" "$node"
            exit 1
        fi
        gum log --structured --level info "Successfully rebooted" "node" "$node"
        ;;

    "Shutdown" | "shutdown")
        check_cli talosctl

        local node="${args[1]}"
        if [[ -z "$node" ]]; then
            node=$(select_node "Select node to shutdown")
        fi

        local node_ip
        node_ip=$(get_node_ip "$node")

        if gum confirm "Shutdown node ${node} (${node_ip})?"; then
            gum log --structured --level info "Shutting down node" "node" "$node"
            if ! talosctl shutdown --nodes "${node_ip}" --force; then
                gum log --structured --level error "Failed to shutdown" "node" "$node"
                exit 1
            fi
            gum log --structured --level info "Successfully shut down" "node" "$node"
        else
            gum log --structured --level info "Shutdown cancelled"
        fi
        ;;

    "Reset" | "reset")
        check_cli talosctl

        local node="${args[1]}"
        if [[ -z "$node" ]]; then
            node=$(select_node "Select node to reset")
        fi

        local node_ip
        node_ip=$(get_node_ip "$node")

        if gum confirm "Reset node ${node} (${node_ip})? This will DESTROY all data!"; then
            gum log --structured --level info "Resetting node" "node" "$node"
            if ! talosctl reset --nodes "${node_ip}" --graceful=false; then
                gum log --structured --level error "Failed to reset" "node" "$node"
                exit 1
            fi
            gum log --structured --level info "Successfully reset" "node" "$node"
        else
            gum log --structured --level info "Reset cancelled"
        fi
        ;;

    "Generate Kubeconfig" | "kubeconfig")
        check_env ROOT_DIR
        check_cli talosctl

        local node_ip
        node_ip=$(get_first_node_ip)

        gum log --structured --level info "Generating kubeconfig" "from_node" "$node_ip"
        if ! talosctl kubeconfig --nodes "${node_ip}" --force --force-context-name main "${ROOT_DIR}"; then
            gum log --structured --level error "Failed to generate kubeconfig"
            exit 1
        fi
        gum log --structured --level info "Successfully generated kubeconfig"
        ;;

    "Rotate Client Certs" | "rotate-certs" | "rotate")
        check_cli op yq talosctl base64 gum

        local node_ip
        node_ip=$(get_first_node_ip)
        local base_config="${TALOS_DIR}/machineconfig.yaml"

        gum log --structured --level info "Rotating client cert"
        op_signin

        local injected
        injected=$(mktemp)
        op inject -i "${base_config}" | envsubst >"${injected}"

        local ca_crt_b64
        local ca_key_b64
        ca_crt_b64=$(yq -r '.machine.ca.crt' "${injected}" | tr -d '\n')
        ca_key_b64=$(yq -r '.machine.ca.key' "${injected}" | tr -d '\n')

        local tmp
        tmp=$(mktemp -d)
        trap 'rm -rf "${tmp}" "${injected}"' EXIT
        echo "${ca_crt_b64}" | base64 -d >"${tmp}/ca.crt"
        echo "${ca_key_b64}" | base64 -d >"${tmp}/ca.key"

        pushd "${tmp}" >/dev/null || return 1
        talosctl gen key --name admin                         # admin.key
        talosctl gen csr --key admin.key --ip 127.0.0.1       # admin.csr
        # Generate admin client certificate with configurable validity (default 1 year)
        talosctl gen crt --ca ca --csr admin.csr --name admin --hours "${ADMIN_CERT_HOURS:-8760}" # admin.crt
        popd >/dev/null || return 1

        local admin_crt_b64
        local admin_key_b64
        admin_crt_b64=$(base64 < "${tmp}/admin.crt" | tr -d '\n')
        admin_key_b64=$(base64 < "${tmp}/admin.key" | tr -d '\n')

        # Determine current talos context (fallback to "main" if unset)
        local current_context
        current_context=$(yq -r '.context' talosconfig)
        if [[ -z "$current_context" || "$current_context" == "null" ]]; then
            current_context="main"
        fi

        CURRENT_CONTEXT="$current_context" ADMIN_CRT_B64="$admin_crt_b64" ADMIN_KEY_B64="$admin_key_b64" yq -i '
          .contexts[env(CURRENT_CONTEXT)].crt = env(ADMIN_CRT_B64) |
          .contexts[env(CURRENT_CONTEXT)].key = env(ADMIN_KEY_B64)
        ' talosconfig

        gum log --structured --level info "Rotation complete – talosconfig updated"
        ;;

    "Node Status" | "status")
        check_cli talosctl yq

        echo ""
        gum style --bold "Cluster Nodes"
        echo ""

        local nodes
        nodes=$(get_node_names)

        for node in $nodes; do
            local ip
            ip=$(get_node_ip "$node")
            local schematic
            schematic=$(basename "$(get_node_schematic "$node")")

            # Try to get node status
            local status="unknown"
            if talosctl --nodes "$ip" version &>/dev/null; then
                status="✓ online"
            else
                status="✗ offline"
            fi

            printf "  %-20s %-16s %-25s %s\n" "$node" "$ip" "$schematic" "$status"
        done

        echo ""
        gum style --bold "Cluster VIP: $(get_cluster_vip)"
        echo ""
        ;;

    "-h" | "--help" | "Help")
        show_help
        ;;

    "Back")
        ops
        ;;

    *)
        gum log --structured --level error "Unknown command" "command" "${args[0]}"
        show_help
        ;;
    esac
}

main "$@"
