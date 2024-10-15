# This file contains useful commands to run when shit has hit the fan

## Run after updating coreDNS to redeploy all pods

kubectl get deployments --all-namespaces -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{.metadata.name}{"\n"}{end}' | xargs -n2 bash -c 'kubectl rollout restart deployment -n $0 $1'
kubectl get daemonsets --all-namespaces -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{.metadata.name}{"\n"}{end}' | xargs -n2 bash -c 'kubectl rollout restart daemonset -n $0 $1'

## MAC address of the NIC for this node

talosctl get links -n <ip> --insecure

## Restoring a CNPG Cluster

1. Change `.spec.backup.barmanObjectStore.serverName` to your new cluster name:

    ```yaml
    serverName: &currentCluster postgres16-02
    ```

    Usually, you just increment this, such as `postgres16-01` -> `postgres16-02`.

2. Change `.spec.bootstrap.recovery.source` to your old cluster name:

    ```yaml
    bootstrap:
      recovery:
        source: &previousCluster postgres16-01
    ```

For instance, your old cluster name is simply the name that is/was within: `.spec.serverName`

In this example we changed from postgres16-01 -> postgres16-02 and restored from postgres16-01

## Fixing Cert Manager

1. Move this directory completely out of your repo. To delete it: kubernetes/apps/cert-manager but make sure its somewhere safe because we will be moving it right back

    Wait for the cert-manager namespace to be gone, check with kubectl get ns

2. Delete the type `ClusterExternalSecret` from your server ex: `kubectl delete ClusterExternalSecret domain-com-tlx`

3. Copy my cert manager config, while making any of your own changes as needed

    https://github.com/Heavybullets8/heavy-ops/tree/main/kubernetes/apps/cert-manager

4. Create kubernetes/apps/network/nginx/certificates
    Ex: https://github.com/Heavybullets8/heavy-ops/tree/main/kubernetes/apps/network/nginx/certificates

5. Change your depends in kubernetes/apps/network/nginx/ks.yaml and add the cert manager

   You can see mine here: https://github.com/Heavybullets8/heavy-ops/blob/main/kubernetes/apps/network/nginx/ks.yaml

6. In kubernetes/apps/network/nginx/external/helmrelease.yaml AND kubernetes/apps/network/nginx/internal/helmrelease.yaml make sure you are using the cert in the network namespace

    Example:

    ```yaml
    default-ssl-certificate: "network/${SECRET_DOMAIN//./-}-tls"
    ```
