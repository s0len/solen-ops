FROM docker.io/nousresearch/hermes-agent:v2026.9.7@sha256:63bfb6d732f49a55d453e801057273785cc61e0f6ee43db3fa2f2a79846301b7

ARG TARGETARCH
# renovate: datasource=github-releases depName=cli/cli
ARG GH_VERSION=v2.100.0
# renovate: datasource=github-releases depName=kubernetes/kubernetes
ARG KUBECTL_VERSION=v1.37.0

RUN set -eu; \
    arch="${TARGETARCH:?TARGETARCH must be set by BuildKit}"; \
    gh_ver="${GH_VERSION#v}"; \
    gh_base="https://github.com/cli/cli/releases/download/v${gh_ver}"; \
    gh_tar="gh_${gh_ver}_linux_${arch}.tar.gz"; \
    curl -fsSL --retry 3 -o "/tmp/${gh_tar}" "${gh_base}/${gh_tar}"; \
    curl -fsSL --retry 3 -o /tmp/gh_checksums.txt "${gh_base}/gh_${gh_ver}_checksums.txt"; \
    grep " ${gh_tar}\$" /tmp/gh_checksums.txt > /tmp/gh.sha256; \
    (cd /tmp && sha256sum -c gh.sha256); \
    tar -xzf "/tmp/${gh_tar}" -C /tmp; \
    install -m 0755 "/tmp/gh_${gh_ver}_linux_${arch}/bin/gh" /usr/local/bin/gh; \
    k_base="https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}"; \
    curl -fsSL --retry 3 -o /tmp/kubectl "${k_base}/kubectl"; \
    curl -fsSL --retry 3 -o /tmp/kubectl.sha256 "${k_base}/kubectl.sha256"; \
    echo "$(cat /tmp/kubectl.sha256)  /tmp/kubectl" | sha256sum -c -; \
    install -m 0755 /tmp/kubectl /usr/local/bin/kubectl; \
    rm -rf /tmp/gh_* /tmp/gh.sha256 /tmp/kubectl /tmp/kubectl.sha256; \
    gh --version; \
    kubectl version --client
