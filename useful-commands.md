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

    [cert-manager](https://github.com/Heavybullets8/heavy-ops/tree/main/kubernetes/apps/cert-manager)

4. Create kubernetes/apps/network/nginx/certificates
    Ex: [certificates](https://github.com/Heavybullets8/heavy-ops/tree/main/kubernetes/apps/network/nginx/certificates)

5. Change your depends in kubernetes/apps/network/nginx/ks.yaml and add the cert manager

   You can see mine here: [ks.yaml](https://github.com/Heavybullets8/heavy-ops/blob/main/kubernetes/apps/network/nginx/ks.yaml)

6. In kubernetes/apps/network/nginx/external/helmrelease.yaml AND kubernetes/apps/network/nginx/internal/helmrelease.yaml make sure you are using the cert in the network namespace

    Example:

    ```yaml
    default-ssl-certificate: "network/${SECRET_DOMAIN//./-}-tls"
    ```

## Creating a zfs pool

1. Create the pod

Create a file named **zfs-tools-pod.yaml** with the following content:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: zfs-tools
  namespace: kube-system
spec:
  containers:
    - name: zfs-tools
      image: ubuntu:22.04
      command: ["/bin/sh", "-c", "apt update && apt install -y zfsutils-linux smartmontools nvme-cli && sleep infinity"]
      securityContext:
        privileged: true
      volumeMounts:
        - name: host-dev
          mountPath: /dev
  volumes:
    - name: host-dev
      hostPath:
        path: /dev
  hostNetwork: true
  hostPID: true
  restartPolicy: Never
```

2. Apply the pod

```sh
kubectl apply -f zfs-tools-pod.yaml
```

3. Shell into the pod

```sh
kubectl exec -it -n kube-system zfs-tools -- bash
```

## Format Disk (Optional)

1. Find The Disk

```sh
lsblk
```

2. You can follow this guide: (https://openzfs.github.io/openzfs-docs/Performance%20and%20Tuning/Hardware.html#nvme-low-level-formatting)[https://openzfs.github.io/openzfs-docs/Performance%20and%20Tuning/Hardware.html#nvme-low-level-formatting]

> This is only really needed if you are using an NVME it appears. But id check out the docs anyway to see if it applies to the SSD you are using. You can skip this step if you want. It doesn't need to be reformatted to the larger sector size.

3. Create a ZFS Pool

```sh
zpool create -o ashift=12 <POOL_NAME> /dev/<DISK>
```

> DISK is gotten from the lsblk command above
> POOL_NAME is anything you want.. i just chose "speed".. eh
> Added ashift=12 since thats what most ssd's use. During pool creation this is also the time to add any other on-creation options you might need. But this is likely it.

4. Verify it's running

```sh
zpool status
```
