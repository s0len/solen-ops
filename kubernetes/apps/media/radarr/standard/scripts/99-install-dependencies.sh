#!/bin/bash
set -e

echo "Installing dependencies..."

# Check if already installed (to make script idempotent)
if command -v jq &> /dev/null && command -v ffmpeg &> /dev/null && command -v dovi_tool &> /dev/null; then
    echo "Dependencies already installed, skipping..."
    exit 0
fi

# Check if apt-get is available
if command -v apt-get &> /dev/null; then
    echo "Using apt-get to install dependencies..."
    apt-get update && \
    apt-get install -y jq ffmpeg wget && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Check if apk is available (Alpine Linux)
elif command -v apk &> /dev/null; then
    echo "Using apk to install dependencies..."
    apk update && \
    apk add jq ffmpeg wget && \
    rm -rf /var/cache/apk/*
else
    echo "No known package manager found (apt-get or apk). Cannot install dependencies."
    exit 1
fi

# Install dovi_tool (static binary)
if ! command -v dovi_tool &> /dev/null; then
    echo "Installing dovi_tool to /usr/local/bin..."
    wget -O /tmp/dovi_tool.tar.gz https://github.com/quietvoid/dovi_tool/releases/download/2.1.2/dovi_tool-2.1.2-x86_64-unknown-linux-musl.tar.gz
    tar -xzf /tmp/dovi_tool.tar.gz -C /usr/local/bin/
    chmod +x /usr/local/bin/dovi_tool
    rm /tmp/dovi_tool.tar.gz
fi

echo "Installation complete!"
echo "Installed versions:"
jq --version 2>&1 || echo "jq: Failed"
ffmpeg -version 2>&1 | head -1 || echo "ffmpeg: Failed"
dovi_tool --version 2>&1 || echo "dovi_tool: Failed"

