#!/usr/bin/env bash

function op_signin() {
    if ! op user get --me &>/dev/null; then
        gum log --structured --level info "Please authenticate with 1Password CLI"
        if ! eval "$(op signin)"; then
            gum log --structured --level error "1Password authentication failed"
            exit 1
        fi
    fi
}

function check_env() {
    local envs=("$@")
    local exit_bool=false
    gum log --structured --level debug "Checking Environment Variables..."
    for env in "${envs[@]}"; do
        if [[ -z "${!env-}" ]]; then
            gum log --structured --level error "\"$env\" unset.."
            exit_bool=true
        else
            gum log --structured --level debug "\"$env\" set.."
        fi
    done
    $exit_bool && exit 1
}

function check_cli() {
    local deps=("$@")
    local exit_bool=false
    gum log --structured --level debug "Checking Dependencies..."
    for dep in "${deps[@]}"; do
        if ! command -v "$dep" &>/dev/null; then
            gum log --structured --level error "\"$dep\" not found.."
            exit_bool=true
        else
            gum log --structured --level debug "\"$dep\" found.."
        fi
    done
    $exit_bool && exit 1
}

function render_template() {
    local file="$1"
    local output
    gum log --structured --level debug "Rendering \"$file\""
    if [[ ! -f "$file" ]]; then
        gum log --structured --level error "File not found" "file" "$file"
        exit 1
    fi
    if ! output=$(op inject -i "$file" 2>/dev/null) || [[ -z "$output" ]]; then
        gum log --structured --level error "Failed to render" "file" "$file"
        exit 1
    fi
    echo "$output"
}

function fetch_kubeconfig() {
    local node_ip="${1:-$(get_first_node_ip)}"
    gum log --structured --level info "Fetching kubeconfig from node ${node_ip}"
    if ! output=$(talosctl kubeconfig --nodes "$node_ip" --force --force-context-name main "$(basename "${KUBECONFIG}")" 2>&1); then
        gum log --structured --level error "Failed to fetch kubeconfig" "output" "$output"
        exit 1
    fi
    gum log --structured --level info "Kubeconfig fetched successfully"
}

function generate_schematic() {
    local schematic_file="${1:-$SCHEMATIC_FILE}"
    gum log --structured --level info "Generating Talos schematic" "file" "$schematic_file"

    if [[ ! -f "$schematic_file" ]]; then
        gum log --structured --level error "Schematic file not found" "file" "$schematic_file"
        exit 1
    fi

    local schematic_id
    if ! schematic_id=$(curl --silent -X POST --data-binary @"$schematic_file" https://factory.talos.dev/schematics | jq --raw-output '.id'); then
        gum log --structured --level error "Failed to generate schematic ID"
        exit 1
    fi

    echo "$schematic_id"
}

# ===== Multi-node helper functions =====

# Get list of all node names from nodes.yaml
function get_node_names() {
    check_cli yq
    if [[ ! -f "$NODES_FILE" ]]; then
        gum log --structured --level error "Nodes file not found" "file" "$NODES_FILE"
        exit 1
    fi
    yq -r '.nodes | keys | .[]' "$NODES_FILE"
}

# Get IP address for a specific node
function get_node_ip() {
    local node_name="$1"
    check_cli yq
    if [[ ! -f "$NODES_FILE" ]]; then
        gum log --structured --level error "Nodes file not found" "file" "$NODES_FILE"
        exit 1
    fi
    yq -r ".nodes.\"${node_name}\".ip" "$NODES_FILE"
}

# Get schematic file for a specific node
function get_node_schematic() {
    local node_name="$1"
    check_cli yq
    if [[ ! -f "$NODES_FILE" ]]; then
        gum log --structured --level error "Nodes file not found" "file" "$NODES_FILE"
        exit 1
    fi
    local schematic
    schematic=$(yq -r ".nodes.\"${node_name}\".schematic" "$NODES_FILE")
    echo "${TALOS_DIR}/${schematic}"
}

# Get the first node IP (for operations that need any node)
function get_first_node_ip() {
    local first_node
    first_node=$(get_node_names | head -1)
    get_node_ip "$first_node"
}

# Get the VIP address
function get_cluster_vip() {
    check_cli yq
    if [[ ! -f "$NODES_FILE" ]]; then
        gum log --structured --level error "Nodes file not found" "file" "$NODES_FILE"
        exit 1
    fi
    yq -r '.vip' "$NODES_FILE"
}

# Interactive node selection
function select_node() {
    local prompt="${1:-Select a node}"
    local nodes
    nodes=$(get_node_names)

    local selected
    selected=$(echo "$nodes" | gum choose --header "$prompt")

    if [[ -z "$selected" ]]; then
        gum log --structured --level error "No node selected"
        exit 1
    fi

    echo "$selected"
}

# Interactive multi-node selection (returns space-separated list)
function select_nodes() {
    local prompt="${1:-Select nodes}"
    local nodes
    nodes=$(get_node_names)

    local selected
    selected=$(echo "$nodes" | gum choose --no-limit --header "$prompt")

    if [[ -z "$selected" ]]; then
        gum log --structured --level error "No nodes selected"
        exit 1
    fi

    echo "$selected"
}

# Get all node IPs as comma-separated list (for talosctl endpoints)
function get_all_node_ips() {
    local nodes
    nodes=$(get_node_names)
    local ips=""

    for node in $nodes; do
        local ip
        ip=$(get_node_ip "$node")
        if [[ -n "$ips" ]]; then
            ips="${ips},${ip}"
        else
            ips="$ip"
        fi
    done

    echo "$ips"
}

# Check if a node config file exists
function node_config_exists() {
    local node_name="$1"
    [[ -f "${TALOS_DIR}/nodes/${node_name}.yaml" ]]
}
