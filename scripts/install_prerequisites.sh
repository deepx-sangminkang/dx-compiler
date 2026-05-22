#!/bin/bash

# Provide 'sudo' shim when not installed (e.g., minimal containers running as root).
if ! command -v sudo >/dev/null 2>&1; then
    sudo() { "$@"; }
fi

echo "Install dependencies..."

# Detect OS family
if [ -f /etc/os-release ]; then
    OS_ID=$(grep "^ID=" /etc/os-release | sed 's/^ID=//' | tr -d '"')
    OS_VERSION_ID=$(grep "^VERSION_ID=" /etc/os-release | sed 's/^VERSION_ID=//' | tr -d '"')
else
    echo "ERROR: /etc/os-release not found. Cannot determine OS." && exit 1
fi

echo "*** OS: ${OS_ID} ${OS_VERSION_ID} ***"

# Determine package manager family
case "$OS_ID" in
    ubuntu|debian)
        # Debian/Ubuntu family - use apt
        UBUNTU_VERSION="${OS_VERSION_ID}"

        sudo apt-get update && sudo apt-get install -y software-properties-common && \
        sudo add-apt-repository -y universe && sudo apt-get update

        # Version-specific packages
        if [ "$UBUNTU_VERSION" = "24.04" ] || [ "$UBUNTU_VERSION" = "26.04" ]; then
            # libfuse2 is safe (needed for AppImages), but 'fuse' package must be avoided
            sudo apt-get install -y --no-install-recommends \
                libgl1-mesa-dev libglib2.0-0 make \
                libfuse2 libayatana-appindicator3-1 \
                libncurses-dev
        elif [ "$UBUNTU_VERSION" = "22.04" ]; then
            sudo apt-get install -y --no-install-recommends \
                libgl1-mesa-dev libglib2.0-0 make \
                libfuse2 libappindicator3-1 libgconf-2-4 \
                libncurses5-dev libncursesw5-dev
        elif [ "$UBUNTU_VERSION" = "20.04" ] || [ "$UBUNTU_VERSION" = "18.04" ]; then
            sudo apt-get install -y --no-install-recommends \
                libgl1-mesa-dev libgl1-mesa-glx libglib2.0-0 make \
                libfuse2 libappindicator1 libgconf-2-4 \
                libncurses5-dev libncursesw5-dev
        elif [ "$OS_ID" = "debian" ]; then
            sudo apt-get install -y --no-install-recommends \
                libgl1-mesa-dev libglib2.0-0 make \
                libfuse2 \
                libncurses-dev
        else
            echo "Unsupported Ubuntu/Debian version: $OS_VERSION_ID" && exit 1
        fi

        # Common packages across all Debian/Ubuntu versions
        sudo apt-get install -y --no-install-recommends \
            libssl-dev \
            wget \
            openssl \
            build-essential \
            zlib1g-dev \
            patchelf \
            libffi-dev \
            ca-certificates \
            libbz2-dev \
            liblzma-dev \
            libsqlite3-dev \
            tk-dev \
            libgdbm-dev \
            libc6-dev \
            libnss3-dev \
            ccache \
            libxss1 libxtst6 libnss3 \
            libcanberra-gtk-module libcanberra-gtk3-module \
            xdg-utils
        ;;

    fedora|rhel|centos)
        # Red Hat family - use dnf
        echo "Installing prerequisites for ${OS_ID} ${OS_VERSION_ID}..."

        OS_MAJOR_VERSION="${OS_VERSION_ID%%.*}"

        # Enable extra repos (CRB/PowerTools + EPEL) for RHEL/CentOS so that
        # devel packages like mesa-libGL-devel, gdbm-devel, nss-devel and
        # EPEL-only packages like patchelf, ccache, xdg-utils are available.
        # Fedora ships these in its default repos, so this step is skipped there.
        if [ "$OS_ID" = "rhel" ] || [ "$OS_ID" = "centos" ]; then
            # Ensure dnf-plugins-core is available (provides 'config-manager')
            sudo dnf install -y dnf-plugins-core 2>/dev/null || true

            # Enable CodeReady Builder / PowerTools (name varies by distro+version)
            sudo dnf config-manager --set-enabled crb 2>/dev/null \
                || sudo dnf config-manager --set-enabled powertools 2>/dev/null \
                || sudo dnf config-manager --set-enabled "codeready-builder-for-rhel-${OS_MAJOR_VERSION}-$(uname -m)-rpms" 2>/dev/null \
                || true

            # Install EPEL for the detected major version (best effort)
            sudo dnf install -y "https://dl.fedoraproject.org/pub/epel/epel-release-latest-${OS_MAJOR_VERSION}.noarch.rpm" 2>/dev/null \
                || sudo dnf install -y epel-release 2>/dev/null \
                || true
        else
            # Fedora: epel-release isn't needed; ignore failure quietly
            sudo dnf install -y epel-release 2>/dev/null || true
        fi

        # Pick a flag that tells dnf to keep going when some packages are
        # unavailable. dnf5 (Fedora 41+) uses --skip-unavailable; older dnf4
        # uses --skip-broken. Probe once and reuse.
        SKIP_FLAG=""
        if sudo dnf install --help 2>/dev/null | grep -q -- '--skip-unavailable'; then
            SKIP_FLAG="--skip-unavailable"
        elif sudo dnf install --help 2>/dev/null | grep -q -- '--skip-broken'; then
            SKIP_FLAG="--skip-broken"
        fi

        sudo dnf install -y ${SKIP_FLAG} \
            mesa-libGL-devel \
            glib2 \
            make \
            fuse-libs \
            ncurses-devel \
            openssl-devel \
            wget \
            openssl \
            gcc gcc-c++ \
            zlib-devel \
            patchelf \
            libffi-devel \
            ca-certificates \
            bzip2-devel \
            xz-devel \
            sqlite-devel \
            tk-devel \
            gdbm-devel \
            glibc-devel \
            nss-devel \
            ccache \
            libXScrnSaver libXtst nss \
            xdg-utils \
            findutils \
            tar \
            gzip \
            lsof \
            iproute \
            net-tools
        ;;

    *)
        echo "Unsupported OS: $OS_ID" && exit 1
        ;;
esac

# Check if the installation was successful
if [ $? -eq 0 ]; then
    echo "Dependencies installed successfully."
else
    echo "Dependency installation failed."
    exit 1
fi
