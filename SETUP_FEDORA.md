# Setup Guide for Fedora Linux Workstation

This document provides a comprehensive guide to set up your Fedora Linux workstation to work with the `solen-ops` GitOps repository.

## Repository Overview

This is a GitOps repository for managing a Kubernetes cluster using:
- **Talos OS** - Kubernetes distribution
- **Flux** - GitOps operator for Kubernetes
- **Helmfile** - For bootstrapping the cluster
- **SOPS** - For secret encryption/decryption
- **Kustomize** - For Kubernetes resource management

## Required Tools

### Core Kubernetes Tools

1. **kubectl** - Kubernetes command-line tool
   ```bash
   sudo dnf install -y kubectl
   ```

2. **helm** - Kubernetes package manager
   ```bash
   sudo dnf install -y helm
   ```

3. **talosctl** - Talos OS management tool
   ```bash
   # Download from GitHub releases
   curl -Lo /tmp/talosctl.tar.gz https://github.com/siderolabs/talos/releases/download/v1.11.5/talosctl-linux-amd64.tar.gz
   tar -xzf /tmp/talosctl.tar.gz -C /tmp
   sudo mv /tmp/talosctl /usr/local/bin/
   chmod +x /usr/local/bin/talosctl
   ```

4. **flux** - GitOps toolkit
   ```bash
   curl -s https://fluxcd.io/install.sh | sudo bash
   ```

### GitOps & Secret Management

5. **helmfile** - Helm deployment manager
   ```bash
   # Install via Go or download binary
   sudo dnf install -y helmfile
   # OR download from GitHub releases
   curl -Lo /tmp/helmfile.tar.gz https://github.com/helmfile/helmfile/releases/latest/download/helmfile_linux_amd64.tar.gz
   tar -xzf /tmp/helmfile.tar.gz -C /tmp
   sudo mv /tmp/helmfile /usr/local/bin/
   chmod +x /usr/local/bin/helmfile
   ```

6. **sops** - Secrets management tool
   ```bash
   # Install via package manager or download binary
   sudo dnf install -y sops
   # OR download from GitHub releases
   curl -Lo /tmp/sops.tar.gz https://github.com/getsops/sops/releases/latest/download/sops-v3.8.1.linux.amd64.tar.gz
   tar -xzf /tmp/sops.tar.gz -C /tmp
   sudo mv /tmp/sops /usr/local/bin/
   chmod +x /usr/local/bin/sops
   ```

7. **age** - Encryption tool (used by SOPS)
   ```bash
   sudo dnf install -y age
   # OR download from GitHub releases
   curl -Lo /tmp/age.tar.gz https://github.com/FiloSottile/age/releases/latest/download/age-v1.1.1-linux-amd64.tar.gz
   tar -xzf /tmp/age.tar.gz -C /tmp
   sudo mv /tmp/age/age /usr/local/bin/
   sudo mv /tmp/age/age-keygen /usr/local/bin/
   chmod +x /usr/local/bin/age*
   ```

### Utility Tools

8. **yq** - YAML processor
   ```bash
   sudo dnf install -y yq
   # OR download from GitHub releases
   curl -Lo /tmp/yq https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64
   sudo mv /tmp/yq /usr/local/bin/
   chmod +x /usr/local/bin/yq
   ```

9. **jq** - JSON processor
   ```bash
   sudo dnf install -y jq
   ```

10. **gum** - CLI tool for interactive prompts (used by task scripts)
    ```bash
    # Download from GitHub releases
    curl -Lo /tmp/gum.tar.gz https://github.com/charmbracelet/gum/releases/latest/download/gum_0.13.0_linux_x86_64.tar.gz
    tar -xzf /tmp/gum.tar.gz -C /tmp
    sudo mv /tmp/gum /usr/local/bin/
    chmod +x /usr/local/bin/gum
    ```

11. **op** - 1Password CLI (optional, if using 1Password for secrets)
    ```bash
    # Follow 1Password CLI installation guide
    # https://developer.1password.com/docs/cli/get-started
    ```

### Development Tools

12. **git** - Version control
    ```bash
    sudo dnf install -y git
    ```

13. **curl** & **wget** - Download tools
    ```bash
    sudo dnf install -y curl wget
    ```

## Cursor IDE Setup

### 1. Install Cursor

Download and install Cursor from [cursor.sh](https://cursor.sh) or use the package manager:

```bash
# Download the .rpm package from cursor.sh
# Then install:
sudo dnf install -y cursor-*.rpm
```

### 2. Recommended Cursor Extensions

Install these extensions in Cursor for better GitOps workflow:

- **Kubernetes** - Kubernetes resource management
- **YAML** - YAML language support
- **SOPS** - SOPS file support (if available)
- **Helm Intellisense** - Helm chart support
- **Flux** - Flux resource support (if available)

### 3. Cursor Settings

Copy or configure these settings in Cursor (`.vscode/settings.json` is already in the repo):

Key settings:
- Kubernetes kubeconfig path: `./kubeconfig`
- YAML schema associations for Kubernetes and Flux resources
- SOPS default age key file: `age.key`

## Repository Configuration

### 1. Clone the Repository

```bash
git clone <repository-url> solen-ops
cd solen-ops
```

### 2. Required Files

Ensure these files exist in the repository root:

- `kubeconfig` - Kubernetes cluster configuration (may need to be fetched)
- `talosconfig` - Talos cluster configuration
- `config.yaml` - Talos machine configuration
- `.talos.env` - Environment variables for Talos/Kubernetes versions
- `age.key` - Age encryption key for SOPS (if using age encryption)
- `github-deploy.key.pub` - GitHub deploy key (public)

### 3. Environment Variables

The repository uses environment variables defined in `.talos.env`:

```bash
# These are set automatically when sourcing .talos.env
KUBERNETES_VERSION=v1.34.3
TALOS_VERSION=v1.11.5
```

### 4. Fetch Kubeconfig (if needed)

If you don't have a `kubeconfig` file, you can fetch it from the cluster:

```bash
# Using the task script (requires talosctl and node IP)
./.tasks/ops kubernetes fetch-kubeconfig

# Or manually:
talosctl kubeconfig --nodes <node-ip> --force --force-context-name main kubeconfig
```

## Task Scripts

The repository includes helper scripts in `.tasks/` directory:

### Available Commands

```bash
# Interactive menu
./.tasks/ops

# Specific commands
./.tasks/ops bootstrap    # Bootstrap operations
./.tasks/ops flux         # Flux operations
./.tasks/ops kubernetes   # Kubernetes operations
./.tasks/ops talos        # Talos operations
./.tasks/ops volsync      # VolSync operations
```

### Task Script Dependencies

The task scripts require:
- `gum` - For interactive prompts
- `yq` - For YAML processing
- `talosctl` - For Talos operations
- `kubectl` - For Kubernetes operations
- `op` (1Password CLI) - If using 1Password for secrets

## Secret Management

### SOPS Configuration

The repository uses SOPS for encrypted secrets. Ensure you have:

1. **Age key file** - `age.key` in the repository root (or configured path)
2. **SOPS configuration** - Usually in `.sops.yaml` or repository root

### 1Password Integration (if used)

If the repository uses 1Password for secrets:

```bash
# Sign in to 1Password
op signin

# The task scripts will handle 1Password authentication automatically
```

## Bootstrap Process

The repository uses Helmfile for bootstrapping. The bootstrap configuration is in `bootstrap/helmfile.yaml`.

### Bootstrap Dependencies

The bootstrap process installs (in order):
1. Cilium (CNI)
2. CoreDNS
3. External Secrets Operator
4. Flux Operator
5. Flux Instance

### Running Bootstrap

```bash
# Using task script
./.tasks/ops bootstrap

# Or manually with helmfile
cd bootstrap
export ROOT_DIR=$(git rev-parse --show-toplevel)
helmfile sync
```

## Common Workflows

### 1. View Cluster Status

```bash
kubectl get nodes
kubectl get pods --all-namespaces
```

### 2. Check Flux Status

```bash
flux get all
flux get kustomizations
flux get helmreleases
```

### 3. Apply Changes

The repository uses GitOps, so changes are typically:
1. Make changes to YAML files
2. Commit and push to Git
3. Flux automatically applies changes

For local testing:

```bash
# Test with flux-local (if installed)
flux test --path kubernetes/flux/cluster
```

### 4. Decrypt and Edit Secrets

```bash
# Edit encrypted file
sops secrets/encrypted-file.yaml

# Or decrypt to view
sops -d secrets/encrypted-file.yaml
```

## Troubleshooting

### Missing kubeconfig

```bash
# Fetch from cluster
talosctl kubeconfig --nodes <node-ip> --force kubeconfig
```

### SOPS Decryption Issues

```bash
# Check age key
ls -la age.key

# Test SOPS
sops -d secrets/encrypted-file.yaml
```

### Task Script Errors

Ensure all dependencies are installed:
```bash
which kubectl talosctl helmfile sops yq jq gum
```

### Flux Sync Issues

```bash
# Check Flux status
flux get all

# Force reconciliation
flux reconcile kustomization <name> --with-source

# Check logs
kubectl logs -n flux-system -l app=flux
```

## Network Configuration

The cluster uses:
- **Control Plane VIP**: 192.168.10.80
- **Node IPs**: 192.168.10.0/24 subnet
- **Pod Subnet**: 10.69.0.0/16
- **Service Subnet**: 10.96.0.0/16

Ensure your workstation can reach the cluster network.

## Additional Resources

- **Talos Documentation**: https://www.talos.dev/
- **Flux Documentation**: https://fluxcd.io/
- **SOPS Documentation**: https://github.com/getsops/sops
- **Useful Commands**: See `useful-commands.md` in the repository

## Quick Verification Checklist

After setup, verify everything works:

- [ ] `kubectl get nodes` - Shows cluster nodes
- [ ] `flux get all` - Shows Flux resources
- [ ] `talosctl version` - Shows Talos version
- [ ] `sops --version` - Shows SOPS version
- [ ] `./.tasks/ops` - Opens interactive menu
- [ ] Cursor can open and navigate the repository
- [ ] Kubernetes extension in Cursor works
- [ ] YAML files have proper syntax highlighting

## Notes

- The repository structure follows GitOps best practices
- Most operations are automated via Flux
- Manual interventions should be minimal
- Always test changes in a safe environment first
- Keep `kubeconfig` and `talosconfig` secure and backed up

