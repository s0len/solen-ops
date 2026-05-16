# Kanidm

Self-hosted identity provider. Will become the user directory for Authelia
(replacing glauth) in a follow-up migration.

Domain: `idm.uniflix.vip` (served via `envoy-external`).

## Initial Setup

### 1. Recover the built-in admin accounts

After the first deploy, recover both built-in accounts on the pod:

```bash
kubectl exec -n security -it statefulset/kanidm -- kanidmd recover-account admin
kubectl exec -n security -it statefulset/kanidm -- kanidmd recover-account idm_admin
```

Each command prints a one-shot recovery password. Use it to set a real
password / passkey via the web UI at <https://idm.uniflix.vip/ui/reset>.

### 2. Create the provision service account

Run these from a workstation with the `kanidm` CLI installed and pointed at
`https://idm.uniflix.vip`:

```bash
kanidm service-account create kanidm-provision "Kanidm Provision" idm_admins -D idm_admin
kanidm group add-members idm_admins kanidm-provision -D idm_admin
kanidm service-account api-token generate --rw kanidm-provision provision-token -D idm_admin
```

Store the printed token in 1Password as item `kanidm-provision` /
field `token`. The `kanidm-provision-token` ExternalSecret will pick it up.

### 3. Enable the provision sidecar

In `app/helmrelease.yaml`, remove the `replicas: 0` override on the
`provision` controller (or change it to `1`) and reconcile:

```bash
flux reconcile helmrelease -n security kanidm
```

### 4. Create groups + your personal account

```bash
kanidm group create users  -D idm_admin
kanidm group create admins -D idm_admin

kanidm person create <username> "<Display Name>" -D idm_admin
kanidm person update <username> --mail "<email>" -D idm_admin
kanidm group add-members users  <username> -D idm_admin
kanidm group add-members admins <username> -D idm_admin
kanidm group add-members idm_admins <username> -D idm_admin

kanidm person credential create-reset-token <username> -D idm_admin
```

Use the reset token to set a password or enroll a passkey at
<https://idm.uniflix.vip>.

## Authelia migration (later)

Authelia and glauth are intentionally untouched. The cutover to
Kanidm-as-LDAP-backend for Authelia is a separate change once Kanidm has
all users provisioned.
