# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Kubernetes home operations repository for a self-hosted bare-metal cluster running on Talos Linux. It uses GitOps principles with Flux CD to manage all deployments declaratively.

**Core Stack:**
- **OS**: Talos Linux (immutable Kubernetes OS)
- **CNI**: Cilium (no kube-proxy)
- **GitOps**: Flux CD with Kustomize
- **Secrets**: SOPS (age encryption) + External-Secrets (1Password sync)
- **Storage**: Rook-Ceph (distributed), OpenEBS (local), ZFS

## Common Commands

### Task Runner (`./ops`)
The primary CLI interface for cluster operations. Run interactively or with subcommands:

```bash
./ops                          # Interactive menu
./ops talos apply <node>       # Apply Talos config to a node
./ops talos apply -i <node>    # Apply to node in maintenance mode (insecure)
./ops talos apply-all          # Apply config to all nodes sequentially
./ops talos upgrade <node>     # Upgrade Talos on a node
./ops talos upgrade-k8s        # Upgrade Kubernetes on all nodes
./ops talos reboot <node>      # Reboot a node
./ops talos status             # Show node status
./ops talos rotate-certs       # Rotate Talos client certificate
./ops talos kubeconfig         # Regenerate kubeconfig
./ops flux reconcile           # Reconcile all Kustomizations/HelmReleases
./ops bootstrap                # Full cluster bootstrap (destructive)
./ops volsync                  # Volume backup operations
```

### Environment
Environment variables are set via `.mise.toml`:
- `KUBECONFIG` → `./kubeconfig`
- `TALOSCONFIG` → `./talosconfig`
- `SOPS_AGE_KEY_FILE` → `./age.key`

Talos/Kubernetes versions are in `.talos.env`.

### Manual Flux Reconciliation
```bash
flux reconcile kustomization -n flux-system cluster
flux reconcile helmrelease -n <namespace> <release>
```

## Architecture

### Directory Structure
```
kubernetes/
├── apps/                  # Applications organized by namespace
│   ├── <namespace>/       # Each namespace has its own directory
│   │   ├── kustomization.yaml   # Lists all apps in this namespace
│   │   └── <app>/
│   │       ├── ks.yaml          # Flux Kustomization(s) for the app
│   │       └── app/             # Actual manifests (helmrelease.yaml, etc.)
├── components/            # Reusable Kustomize components
│   ├── common/            # Shared resources (namespaces, repos, secrets, vars)
│   ├── gatus/             # Status monitoring (external/guarded)
│   ├── gpu/               # GPU resource claiming
│   └── volsync/           # Backup component (adds PVC + ReplicationSource)
└── flux/                  # Flux configuration
    └── cluster/ks.yaml    # Main cluster Kustomization

talos/
├── machineconfig.yaml     # Base machine config (uses 1Password op:// refs)
├── nodes.yaml             # Node definitions (IP, schematic per node)
├── nodes/<node>.yaml      # Per-node config patches
├── patches/               # Additional patches
└── schematic*.yaml        # Talos factory schematics

bootstrap/
├── helmfile.yaml          # Bootstrap Helm releases (cilium, coredns, external-secrets, flux)
└── resources.yaml.j2      # Bootstrap resources template
```

### App Pattern
Each application follows this structure:
1. `ks.yaml` - Flux Kustomization defining the app, its dependencies, and postBuild substitutions
2. `app/` directory containing:
   - `helmrelease.yaml` - HelmRelease resource
   - `helm-values.yaml` - Helm values (referenced by HelmRelease)
   - Any additional resources (ConfigMaps, Secrets, etc.)

Components are included via the Kustomization's `components:` field:
```yaml
spec:
  components:
    - ../../../../components/volsync    # Adds backup capability
    - ../../../../components/gatus/external  # Adds monitoring
```

### Secrets Management
- **SOPS**: Encrypt files matching `kubernetes/.+\.sops\.yaml` with age
- **External-Secrets**: Syncs secrets from 1Password to Kubernetes
- **1Password refs in Talos**: `machineconfig.yaml` uses `op://vault/item/field` syntax

### Variable Substitution
Flux Kustomizations use `postBuild.substitute` for templating:
- `APP` - Application name
- `VOLSYNC_CAPACITY`, `VOLSYNC_STORAGECLASS`, etc. - Backup settings
- Cluster-wide vars from `kubernetes/components/common/vars/`

## Key Files

- `useful-commands.md` - Emergency runbooks (cert fixes, DB restore, ZFS operations)
- `.renovaterc.json5` - Dependency update configuration
- `config.yaml` - Generated Talos cluster config (don't edit directly)

## Conventions

- Apps are disabled by commenting out their `ks.yaml` reference in the namespace's `kustomization.yaml`
- SOPS-encrypted files use `.sops.yaml` extension
- HelmReleases reference values from separate `helm-values.yaml` files
- Node-specific configs go in `talos/nodes/<node>.yaml`
