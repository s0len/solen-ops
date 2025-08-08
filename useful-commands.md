# This file contains useful commands to run when shit has hit the fan

## Snabb certifikatfix - kommandolista

### 1. Diagnostisera problemet

```bash
# Kolla utgångsdatum för certifikat i network namespace (det som nginx använder)
kubectl get secret domain-com-tls -n network -o json | jq -r '.data."tls.crt"' | base64 -d | openssl x509 -noout -dates

# Kolla utgångsdatum för certifikat i cert-manager namespace (det förnyade)
kubectl get secret domain-com-tls -n cert-manager -o json | jq -r '.data."tls.crt"' | base64 -d | openssl x509 -noout -dates
```

### 2. Kopiera nytt certifikat (om cert-manager har nyare)

```bash
# Ta bort gamla utgångna certifikatet
kubectl delete secret domain-com-tls -n network

# Kopiera det nya certifikatet från cert-manager till network namespace
kubectl get secret domain-com-tls -n cert-manager -o yaml | \
  sed 's/namespace: cert-manager/namespace: network/' | \
  sed '/resourceVersion/d' | \
  sed '/uid:/d' | \
  sed '/creationTimestamp/d' | \
  kubectl apply -f -
```

### 3. Starta om services för att plocka upp nya certifikatet

```bash
# Starta om nginx controllers
kubectl rollout restart deployment nginx-external-controller -n network
kubectl rollout restart deployment nginx-internal-controller -n network

# Starta om cloudflared
kubectl rollout restart deployment cloudflared -n network
```

### 4. Verifiera att det fungerar

```bash
# Kolla att pods startar om korrekt
kubectl get pods -n network | grep -E "(nginx|cloudflared)"

# Kolla cloudflared-loggar för att se att certifikatfelen är borta
kubectl logs -n network deployment/cloudflared --tail=10
```

## En-kommando-fix (om du är säker på problemet)

```bash
# Allt i ett svep
kubectl delete secret domain-com-tls -n network && \
kubectl get secret domain-com-tls -n cert-manager -o yaml | \
  sed 's/namespace: cert-manager/namespace: network/' | \
  sed '/resourceVersion/d' | sed '/uid:/d' | sed '/creationTimestamp/d' | \
  kubectl apply -f - && \
kubectl rollout restart deployment nginx-external-controller nginx-internal-controller cloudflared -n network
```

Detta bör fixa 502-felen och certifikatproblemen på några minuter!

## When vip-tls is bonkers and needs a delete

```sh
kubectl delete externalsecret -n cert-manager domain-com-tls
```

## Run after updating coreDNS to redeploy all pods

```sh
kubectl get deployments --all-namespaces -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{.metadata.name}{"\n"}{end}' | xargs -n2 bash -c 'kubectl rollout restart deployment -n $0 $1'
```

```sh
kubectl get daemonsets --all-namespaces -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{.metadata.name}{"\n"}{end}' | xargs -n2 bash -c 'kubectl rollout restart daemonset -n $0 $1'
```

## How to drop a database

1. Delete the app by uncommenting it from your kustomization.

2. Delete it from your bucket

    ```sh
    aws s3 rm s3://backup/nextcloud --endpoint-url https://{SECRET_-_ID}.r2.cloudflarestorage.com --recursive --dryrun
    ```

3. k9s into CNPG pod and delete the database for plex

should be a pod like:

```txt
postgres16-1
```

type:

```sh
psql
```

Then type:

```sh
\l
```

> press `q` to exit

Find the name of the database to drop. then drop it with:

```sh
DROP DATABASE your_database_name;
```

ex:

```sh
DROP DATABASE nextcloud;
```

1. Exit k9s. Redeploy nextcloud

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
          command:
              [
                  "/bin/sh",
                  "-c",
                  "apt update && apt install -y zfsutils-linux smartmontools nvme-cli && sleep infinity",
              ]
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

## When you need to start a pod and freeze it

Perfect when you want to deploy a pod which crashed becouse it required authentication before being able to start.

```yaml
command:
    - sleep
    - infinity
```

## pod with tools for zfs

```yaml
apiVersion: v1
kind: Pod
metadata:
    name: zfs-tools
    namespace: kube-system
spec:
    hostNetwork: true
    hostPID: true
    containers:
        - name: zfs-tools
          image: ubuntu:22.04
          command:
              - /bin/bash
              - -c
              - |
                  apt update && \
                  apt install -y --no-install-recommends zfsutils-linux smartmontools nvme-cli gdisk && \
                  export PATH=$PATH:/sbin:/usr/sbin && \
                  sleep infinity
          securityContext:
              privileged: true
          volumeMounts:
              - name: dev
                mountPath: /dev
              - name: sys
                mountPath: /sys
              - name: run
                mountPath: /run
              - name: resolv
                mountPath: /etc/resolv.conf
                readOnly: true
    volumes:
        - name: dev
          hostPath:
              path: /dev
              type: Directory
        - name: sys
          hostPath:
              path: /sys
              type: Directory
        - name: run
          hostPath:
              path: /run
              type: Directory
        - name: resolv
          hostPath:
              path: /etc/resolv.conf
              type: File
    restartPolicy: Never
```

## If you run into an issue where cluster-secrets are not being created

```sh
sops -d cluster-secrets.sops.yaml | kubectl apply -f -
```
