#!/bin/bash

# Use custom install path if provided, otherwise default to /usr/local/bin
INSTALL_PATH="${INSTALL_PATH:-/usr/local/bin}"
echo "Installing dependencies to ${INSTALL_PATH}"

# Check if apt-get is available
if command -v apt-get &> /dev/null; then
    echo "Using apt-get to install dependencies..."
    apt-get update && \
    apt-get install -y jq ffmpeg wget && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

    # Copy binaries to custom install path if different from default locations
    if [ "${INSTALL_PATH}" != "/usr/local/bin" ] && [ "${INSTALL_PATH}" != "/usr/bin" ]; then
        echo "Copying binaries to ${INSTALL_PATH}..."
        cp /usr/bin/jq "${INSTALL_PATH}/" 2>/dev/null || true
        cp /usr/bin/ffmpeg "${INSTALL_PATH}/" 2>/dev/null || true
        cp /usr/bin/ffprobe "${INSTALL_PATH}/" 2>/dev/null || true
        cp /usr/bin/wget "${INSTALL_PATH}/" 2>/dev/null || true
    fi

# Check if apk is available (Alpine Linux)
elif command -v apk &> /dev/null; then
    echo "Using apk to install dependencies..."
    apk update && \
    apk add jq ffmpeg wget && \
    rm -rf /var/cache/apk/*

    # Copy binaries to custom install path if different from default locations
    if [ "${INSTALL_PATH}" != "/usr/local/bin" ] && [ "${INSTALL_PATH}" != "/usr/bin" ]; then
        echo "Copying binaries to ${INSTALL_PATH}..."
        cp /usr/bin/jq "${INSTALL_PATH}/" 2>/dev/null || true
        cp /usr/bin/ffmpeg "${INSTALL_PATH}/" 2>/dev/null || true
        cp /usr/bin/ffprobe "${INSTALL_PATH}/" 2>/dev/null || true
        cp /usr/bin/wget "${INSTALL_PATH}/" 2>/dev/null || true
    fi
else
    echo "No known package manager found (apt-get or apk). Cannot install dependencies."
    exit 1
fi

# Install dovi_tool
echo "Installing dovi_tool to ${INSTALL_PATH}..."
wget -O /tmp/dovi_tool.tar.gz https://github.com/quietvoid/dovi_tool/releases/download/2.1.2/dovi_tool-2.1.2-x86_64-unknown-linux-musl.tar.gz
tar -xzf /tmp/dovi_tool.tar.gz -C "${INSTALL_PATH}/"
rm /tmp/dovi_tool.tar.gz

# Make all binaries executable
chmod +x "${INSTALL_PATH}"/*

echo "Installation complete. Binaries available in ${INSTALL_PATH}:"
ls -lh "${INSTALL_PATH}"

