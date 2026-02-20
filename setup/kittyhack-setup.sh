#!/bin/bash

# Color codes
RED='\e[31m'
GREEN='\e[32m'
BLUE='\e[34m'
CYAN='\e[36m'
GREY='\e[37m'
YELLOW='\e[33m'
NC='\e[39m\e[49m' # No color

# Format codes
FMTBOLD='\e[1m'
FMTDEF='\e[0m' # default format

# Set up logging to both terminal and file
LOGPATH="/var/log/kittyhack-setup-$(date +%Y%m%d-%H%M%S)"
LOGFILE="${LOGPATH}/kittyhack-setup.log"
mkdir -p "$LOGPATH"
exec > >(tee -a ${LOGFILE}) 2>&1

# Ensure the script is running as root
if [ "$EUID" -ne 0 ]; then
    echo "This script must be run as root"
    SCRIPT_PATH=$(realpath "$0")
    SCRIPT_NAME=$(basename "$SCRIPT_PATH")
    echo -e "Please start the script with '${CYAN}sudo $SCRIPT_PATH${NC}'"
    exit 1
fi

# Run apt/dpkg non-interactively to avoid prompts blocking the script
export DEBIAN_FRONTEND=noninteractive

# Log apt package information
zgrep "install\|remove" /var/log/dpkg.log* > "${LOGPATH}/apt_changes.log"
dpkg-query -W > "${LOGPATH}/installed_packages_before_kittyhack_setup.log"
cat /var/log/apt/history.log > "${LOGPATH}/apt_history.log"

# Log kernel information
dmesg > "${LOGPATH}/kernel.log"
uname -r > "${LOGPATH}/kernel_version.log"
systemctl list-units --type=service --all > "${LOGPATH}/systemd-services.log"

# Get the current IP address
CURRENT_IP=$(hostname -I | awk '{print $1}')

FAIL_COUNT=0

INSTALL_LEGACY_KITTYHACK=1

# Optional: install a specific git ref (tag/commit) instead of the latest tagged release.
KITTYHACK_VERSION_REF=""

# Optional: uninstall mode
DO_UNINSTALL=false

# Function to check if a service is active
is_service_active() {
    systemctl is-active --quiet "$1"
}

# Function to check if a line in crontab is already commented
is_cron_line_commented() {
    local pattern=$1
    sudo crontab -l 2>/dev/null | grep -E "^#.*${pattern}" > /dev/null
}

# Function to disable services
disable_service() {
    local SERVICE="$1"
    if is_service_active "$SERVICE"; then
        echo -e "${GREY}Stopping and disabling ${SERVICE} service...${NC}"
        systemctl stop "$SERVICE"
        systemctl disable "$SERVICE"
        systemctl daemon-reload
        
        if is_service_active "$SERVICE"; then
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to disable ${SERVICE} service.${NC}"
            echo -e "${RED}WARNING: This would lead to an interference with the KittyHack service!${NC}"
        fi
        echo -e "${GREEN}${SERVICE} service disabled.${NC}"
    else
        echo -e "${GREY}${SERVICE} service is already disabled or inactive.${NC}"
    fi
}

mask_service() {
    local SERVICE="$1"
    if is_service_active "$SERVICE"; then
        echo -e "${GREY}Stopping and masking ${SERVICE} service...${NC}"
        systemctl stop "$SERVICE"
        systemctl disable "$SERVICE"
        systemctl mask "$SERVICE"
        systemctl daemon-reload
        
        if is_service_active "$SERVICE"; then
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to mask ${SERVICE} service.${NC}"
            echo -e "${RED}WARNING: This would lead to an interference with the KittyHack service!${NC}"
        fi
        echo -e "${GREEN}${SERVICE} service masked.${NC}"
    else
        echo -e "${GREY}${SERVICE} service is already masked or inactive.${NC}"
        systemctl mask "$SERVICE"
    fi
}

# Function to enable and start services
enable_service() {
    local SERVICE="$1"
    if ! is_service_active "$SERVICE"; then
        echo -e "${GREY}Enabling and starting ${SERVICE} service...${NC}"
        systemctl daemon-reload
        systemctl enable "$SERVICE"
        systemctl start "$SERVICE"
        if is_service_active "$SERVICE"; then
            echo -e "${GREEN}${SERVICE} service enabled and started successfully.${NC}"
        else
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to start ${SERVICE} service.${NC}"
        fi
    else
        echo -e "${GREY}${SERVICE} service is already enabled and running.${NC}"
    fi
}

# Write the kwork service file
write_kwork_service() {
    cat << EOF > /etc/systemd/system/kwork.service
[Unit]
Description=Main Kittyflap program: Run main server and main program for managing the kittyflap components

[Service]
ExecStart=/root/kittyflap_versions/latest/main
WorkingDirectory=/root/kittyflap_versions/latest/
Environment="HOME=/root/"
Restart=always
RestartSec=1
RuntimeMaxSec=3600s

[Install]
WantedBy=multi-user.target
EOF
}

# Full installation process
install_full() {
    echo -e "${CYAN}--- BASE INSTALL: Check internet connection ---${NC}"
    if ! ping -c 1 google.com &>/dev/null; then
        echo -e "${RED}No internet connection detected. Please check your connection and try again.${NC}"
        exit 1
    else
        echo -e "${GREEN}Internet connection is active.${NC}"
    fi

    echo -e "${CYAN}--- BASE INSTALL Step 1: Stop kittyhack service ---${NC}"
    if systemctl is-active --quiet kittyhack.service; then
        systemctl stop kittyhack.service
        if systemctl is-active --quiet kittyhack.service; then
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to stop KittyHack service.${NC}"
        else
            echo -e "${GREEN}KittyHack service stopped successfully.${NC}"
        fi
    else
            echo -e "${GREY}KittyHack service is already inactive.${NC}"
    fi

    echo -e "${CYAN}--- BASE INSTALL Step 2: Disable unwanted services ---${NC}"
    mask_service "remote-iot"
    mask_service "manager"
    disable_service "kwork"

    # Now rename the manager and dependencies executable file just to be sure
    patterns=(
        "/root/kittyflap_versions/*/manager"
        "/root/kittyflap_versions/*/dependencies"
        "/root/kittyflap_versions/*/main"
    )
    for pattern in "${patterns[@]}"; do
        for path in $(find /root/kittyflap_versions -type f -name "$(basename $pattern)"); do
            if [ -f "$path" ]; then
                mv "$path" "${path}_disabled"
                echo -e "${GREEN}$(basename $path) executable renamed to ${path}_disabled.${NC}"
            else
                echo -e "${GREY}$(basename $path) executable not found. Skipping.${NC}"
            fi
        done
    done

    # Rename also the main manager executable
    if [ -f /root/manager ]; then
        mv /root/manager /root/manager_disabled
        echo -e "${GREEN}Manager main executable renamed to main_disabled.${NC}"
    else
        echo -e "${GREY}Manager main executable not found. Skipping.${NC}"
    fi

    echo -e "${CYAN}--- BASE INSTALL Step 3: Release the magnets, if they are active ---${NC}"
    # Export GPIOs
    echo 525 > /sys/class/gpio/export 2>/dev/null
    echo 524 > /sys/class/gpio/export 2>/dev/null

    # Configure GPIO directions
    echo out > /sys/devices/platform/soc/fe200000.gpio/gpiochip0/gpio/gpio525/direction
    echo out > /sys/devices/platform/soc/fe200000.gpio/gpiochip0/gpio/gpio524/direction

    # Set default output values for GPIOs
    echo -e "${GREY}Releasing the magnet at GPIO525...${NC}"
    sleep 1
    echo 0 > /sys/devices/platform/soc/fe200000.gpio/gpiochip0/gpio/gpio525/value
    echo -e "${GREY}Releasing the magnet at GPIO524...${NC}"
    sleep 1
    echo 0 > /sys/devices/platform/soc/fe200000.gpio/gpiochip0/gpio/gpio524/value

    echo -e "${CYAN}--- BASE INSTALL Step 4: Check and resize swapfile if necessary ---${NC}"
    swapfile_size=$(stat -c%s /swapfile)
    if (( swapfile_size > 2147483648 )); then
        echo -e "${GREEN}Swapfile size is greater than 2GB. Resizing...${NC}"
        sudo swapoff /swapfile
        sudo rm /swapfile
        sudo fallocate -l 2G /swapfile
        sudo chmod 600 /swapfile
        sudo mkswap /swapfile
        sudo swapon /swapfile
        sudo swapon --show | grep -q '/swapfile'
        if [[ $? -ne 0 ]]; then
            ((FAIL_COUNT++))
            echo -e "${YELLOW}Failed to resize and mount swapfile.${NC}"
        else
            echo -e "${GREEN}Swapfile resized to 2GB and mounted successfully.${NC}"
        fi
        echo -e "${GREEN}Swapfile resized to 2GB.${NC}"
    else
        echo -e "${GREY}Swapfile size is 2GB or less. No action needed.${NC}"
    fi

    echo -e "${CYAN}--- BASE INSTALL Step 5: Comment out unwanted crontab entries ---${NC}"
    # Backup and comment out specific crontab lines
    sudo crontab -l > /tmp/current_cron
    update_crontab=false
    if is_cron_line_commented "remoteiot.com/install/upgrade.sh"; then
        echo -e "${GREY}Cron job for remote-iot is already disabled. Skipping.${NC}"
    else
        sed -i '/remoteiot.com\/install\/upgrade.sh/s/^/#/' /tmp/current_cron
        update_crontab=true
        echo -e "${GREEN}Disabled cron job for remote-iot.${NC}"
    fi

    if is_cron_line_commented "/root/manager -update-version"; then
        echo -e "${GREY}Cron job for manager update is already disabled. Skipping.${NC}"
    else
        sed -i '/root\/manager -update-version/s/^/#/' /tmp/current_cron
        update_crontab=true
        echo -e "${GREEN}Disabled cron job for manager update.${NC}"
    fi

    if is_cron_line_commented "/root/manager -manager-start"; then
        echo -e "${GREY}Cron job for manager start is already disabled. Skipping.${NC}"
    else
        sed -i '/root\/manager -manager-start/s/^/#/' /tmp/current_cron
        update_crontab=true
        echo -e "${GREEN}Disabled cron job for manager start.${NC}"
    fi

    if $update_crontab; then
        sudo crontab /tmp/current_cron
        echo -e "${GREEN}Updated crontab.${NC}"
    fi

    rm -f /tmp/current_cron

    echo -e "${CYAN}--- BASE INSTALL Step 6: Rename remote-iot paths ---${NC}"
    if [[ -d /etc/remote-iot ]]; then
        sudo mv /etc/remote-iot /etc/remote-iot-backup
        echo -e "${GREEN}Renamed /etc/remote-iot to /etc/remote-iot-backup.${NC}"
    else
        echo -e "${GREY}suspicious folder /etc/remote-iot not found. Great!"
    fi
    if [[ -f /etc/remote-iot.tar.gz ]]; then
        sudo mv /etc/remote-iot.tar.gz /etc/remote-iot-backup.tar.gz
        echo -e "${GREEN}Renamed /etc/remote-iot.tar.gz to /etc/remote-iot-backup.tar.gz.${NC}"
    else
        echo -e "${GREY}suspicious file /etc/remote-iot.tar.gz not found. Great!"
    fi

    echo -e "${CYAN}--- BASE INSTALL Step 7: Clean up old manager logs ---${NC}"
    if sudo rm -f /var/log/manager_start /var/log/manager_update; then
        echo -e "${GREEN}Manager logs cleaned up.${NC}"
    else
        echo -e "${GREY}No manager logs found. Skipping.${NC}"
    fi
    if sudo rm -f /var/log/manager_start*.gz /var/log/manager_update*.gz; then
        echo -e "${GREEN}Manager logs cleaned up.${NC}"
    else
        echo -e "${GREY}No archived manager logs found. Skipping.${NC}"
    fi

    echo -e "${CYAN}--- BASE INSTALL Step 6: Install gstreamer1.0 packages ---${NC}"
    # Wait for any existing package operations to complete
    while fuser /var/lib/dpkg/lock >/dev/null 2>&1 || fuser /var/lib/apt/lists/lock >/dev/null 2>&1 || fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
        echo -e "${GREY}Waiting for other package operations to complete...${NC}"
        sleep 5
    done

    # Try to update package lists with retries
    for i in {1..5}; do
        if apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 update; then
            break
        else
            echo -e "${YELLOW}Attempt $i of 5 failed. Retrying in 5 seconds...${NC}"
            sleep 5
        fi
    done

    # Install/update ca-certificates first to ensure secure package downloads
    for i in {1..5}; do
        if apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 install -y ca-certificates; then
            break
        else
            echo -e "${YELLOW}Attempt $i of 5 to install ca-certificates failed. Retrying in 5 seconds...${NC}"
            sleep 5
        fi
    done

    GSTREAMER_PACKAGES=(
        gstreamer1.0-tools
        gstreamer1.0-plugins-base
        gstreamer1.0-plugins-good
        gstreamer1.0-libcamera
    )
    echo -e "${GREY}Installing all GStreamer packages...${NC}"
    # Wait for any existing package operations to complete
    while fuser /var/lib/dpkg/lock >/dev/null 2>&1 || fuser /var/lib/apt/lists/lock >/dev/null 2>&1 || fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
        echo -e "${GREY}Waiting for other package operations to complete...${NC}"
        sleep 5
    done

    # Try to install packages with retries
    for i in {1..5}; do
        if apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 install -y "${GSTREAMER_PACKAGES[@]}"; then
            break
        else
            echo -e "${YELLOW}Attempt $i of 5 failed. Retrying in 5 seconds...${NC}"
            sleep 5
        fi
    done

    for pkg in "${GSTREAMER_PACKAGES[@]}"; do
        if dpkg -l | grep -q "$pkg"; then
            echo -e "${GREEN}$pkg installed successfully.${NC}"
        else
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to install $pkg.${NC}"
        fi
        sleep 0.1
    done


    echo -e "${CYAN}--- BASE INSTALL Step 8: Install python ---${NC}"
    PYTHON_PACKAGES=(
        python3
        python3-venv
        python3-dev
        python3-pip
    )
    echo -e "${GREY}Installing all Python packages...${NC}"
    # Wait for any existing package operations to complete
    while fuser /var/lib/dpkg/lock >/dev/null 2>&1 || fuser /var/lib/apt/lists/lock >/dev/null 2>&1 || fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
        echo -e "${GREY}Waiting for other package operations to complete...${NC}"
        sleep 5
    done

    # Try to install packages with retries
    for i in {1..5}; do
        if apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 install -y "${PYTHON_PACKAGES[@]}"; then
            break
        else
            echo -e "${YELLOW}Attempt $i of 5 failed. Retrying in 5 seconds...${NC}"
            sleep 5
        fi
    done
    for pkg in "${PYTHON_PACKAGES[@]}"; do
        if dpkg -l | grep -q "$pkg"; then
            echo -e "${GREEN}$pkg installed successfully.${NC}"
        else
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to install $pkg.${NC}"
        fi
        sleep 0.1
    done

    if python3 -m pip --version &>/dev/null; then
        echo -e "${GREEN}Python pip is installed.${NC}"
    else
        ((FAIL_COUNT++))
        echo -e "${RED}Python pip is not available.${NC}"
    fi

    echo -e "${CYAN}--- BASE INSTALL Step 9: Install git ---${NC}"
    if ! git --version &>/dev/null; then
        echo -e "${GREY}Git is not installed. Installing...${NC}"
        # Wait for any existing package operations to complete
        while fuser /var/lib/dpkg/lock >/dev/null 2>&1 || fuser /var/lib/apt/lists/lock >/dev/null 2>&1 || fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
            echo -e "${GREY}Waiting for other package operations to complete...${NC}"
            sleep 5
        done

        # Try to install git with retries
        for i in {1..5}; do
            if apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 install -y git; then
                break
            else
                echo -e "${YELLOW}Attempt $i of 5 failed. Retrying in 5 seconds...${NC}"
                sleep 5
            fi
        done

        if git --version &>/dev/null; then
            echo -e "${GREEN}Git installed successfully.${NC}"
        else
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to install Git.${NC}"
        fi
    else
        echo -e "${GREEN}Git is already installed.${NC}"
    fi

    echo -e "${CYAN}--- BASE INSTALL Step 10: Install curl and tar ---${NC}"
    if ! curl --version &>/dev/null; then
        echo -e "${GREY}Curl is not installed. Installing...${NC}"
        # Wait for any existing package operations to complete
        while fuser /var/lib/dpkg/lock >/dev/null 2>&1 || fuser /var/lib/apt/lists/lock >/dev/null 2>&1 || fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
            echo -e "${GREY}Waiting for other package operations to complete...${NC}"
            sleep 5
        done

        # Try to install curl with retries
        for i in {1..5}; do
            if apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 install -y curl; then
                break
            else
                echo -e "${YELLOW}Attempt $i of 5 failed. Retrying in 5 seconds...${NC}"
                sleep 5
            fi
        done

        if curl --version &>/dev/null; then
            echo -e "${GREEN}Curl installed successfully.${NC}"
        else
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to install Curl.${NC}"
        fi
    else
        echo -e "${GREEN}Curl is already installed.${NC}"
    fi

    if ! tar --version &>/dev/null; then
        echo -e "${GREY}Tar is not installed. Installing...${NC}"
        # Wait for any existing package operations to complete
        while fuser /var/lib/dpkg/lock >/dev/null 2>&1 || fuser /var/lib/apt/lists/lock >/dev/null 2>&1 || fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
            echo -e "${GREY}Waiting for other package operations to complete...${NC}"
            sleep 5
        done

        # Try to install tar with retries
        for i in {1..5}; do
            if apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 install -y tar; then
                break
            else
                echo -e "${YELLOW}Attempt $i of 5 failed. Retrying in 5 seconds...${NC}"
                sleep 5
            fi
        done

        if tar --version &>/dev/null; then
            echo -e "${GREEN}Tar installed successfully.${NC}"
        else
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to install Tar.${NC}"
        fi
    else
        echo -e "${GREEN}Tar is already installed.${NC}"
    fi

    echo -e "${CYAN}--- BASE INSTALL Step 11: Install OpenCV runtime libs ---${NC}"
    # Some OpenCV wheels require libGL at import time, even in headless setups.
    for i in {1..5}; do
        if apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 install -y libgl1 libglib2.0-0; then
            break
        else
            echo -e "${YELLOW}Attempt $i of 5 failed. Retrying in 5 seconds...${NC}"
            sleep 5
        fi
    done

    if ldconfig -p 2>/dev/null | grep -q "libGL.so.1"; then
        echo -e "${GREEN}OpenCV runtime libs installed.${NC}"
    else
        echo -e "${YELLOW}OpenCV runtime libs may be missing (libGL.so.1 not found in ldconfig).${NC}"
    fi

    install_kittyhack
}

# Remote-mode installation (e.g. amd64 Debian): no Raspberry Pi camera steps.
install_remote_mode() {
    echo -e "${CYAN}--- REMOTE MODE INSTALL: Check internet connection ---${NC}"
    if ! ping -c 1 google.com &>/dev/null; then
        if $USE_LOCAL_SOURCES; then
            echo -e "${YELLOW}No internet connection detected. Continuing because --use-local-sources was set.${NC}"
        else
            echo -e "${RED}No internet connection detected. Please check your connection and try again.${NC}"
            exit 1
        fi
    fi

    echo -e "${CYAN}--- REMOTE MODE INSTALL: Summary ---${NC}"
    if [ "$LANGUAGE" == "de" ]; then
        echo -e "HINWEIS: Die Installation erfordert Root-Rechte. Es wird dringend empfohlen, sie auf einem separaten System zu installieren, das ausschließlich als Kittyhack Remote-Controller dient."
        echo -e ""
        echo -e "Systemanforderungen:"
        echo -e "  - Debian (oder Derivat, z.B. Ubuntu)"
        echo -e "  - Möglichst ein sauberes System oder eine VM"
        echo -e "  - AMD64 (normale 64-bit Intel/AMD PCs, nicht Raspberry Pi/ARM)"
        echo -e "  - Mindestens 2GB RAM"
        echo -e "Folgende Pakete werden installiert:"
        echo -e "  - python3, python3-venv, python3-pip"
        echo -e "  - rsync, git, curl, ca-certificates"
        echo -e "  - libgl1, libglib2.0-0"
        echo -e "Zusatzlich: Es wird ein Python 3.11 Virtualenv erstellt und die Abhangigkeiten aus requirements_remote.txt installiert."
        read -r -p "Mit diesen Installationen fortfahren? (y/N): " CONFIRM_REMOTE_MODE
    else
        echo -e "NOTE: The installation requires root rights. It is strongly recommended to install it on an separate system, which acts only as a kittyhack remote controller."
        echo -e ""
        echo -e "System requirements:"
        echo -e "  - Debian (or derivative, e.g. Ubuntu)"
        echo -e "  - Ideally a clean system or VM"
        echo -e "  - AMD64 (regular 64-bit Intel/AMD PCs, not Raspberry Pi/ARM)"
        echo -e "  - At least 2GB RAM"
        echo -e "The following packages will be installed:"
        echo -e "  - python3, python3-venv, python3-pip"
        echo -e "  - rsync, git, curl, ca-certificates"
        echo -e "  - libgl1, libglib2.0-0"
        echo -e "Additionally: A Python 3.11 virtualenv will be created and dependencies from requirements_remote.txt will be installed."
        read -r -p "Continue with these installations? (y/N): " CONFIRM_REMOTE_MODE
    fi

    case "${CONFIRM_REMOTE_MODE,,}" in
        y|yes|j|ja)
            ;;
        *)
            echo -e "${YELLOW}Remote-mode installation cancelled by user.${NC}"
            exit 0
            ;;
    esac

    # Use already computed source dir (supports --sources-dir)
    KITTYHACK_INSTALL_DIR="$KITTYHACK_ROOT"
    echo -e "${CYAN}Kittyhack path: ${KITTYHACK_ROOT}${NC}"

    echo -e "${CYAN}--- REMOTE MODE INSTALL Step 1: Install system packages ---${NC}"
    apt-get update
    # libgl1 is required by some OpenCV wheels (even in headless/container setups)
    apt-get install -y python3 python3-venv python3-pip rsync git curl ca-certificates libgl1 libglib2.0-0

    echo -e "${CYAN}--- REMOTE MODE INSTALL Step 2: Create virtualenv + install Python deps ---${NC}"
    cd "$KITTYHACK_ROOT" || exit 1
    if ! create_venv_py311 .venv; then
        echo -e "${RED}Failed to create Python 3.11 virtualenv. Aborting.${NC}"
        exit 1
    fi
    source .venv/bin/activate
    pip install --upgrade pip
    pip install --timeout 120 --retries 10 -r requirements_remote.txt

    echo -e "${CYAN}--- REMOTE MODE INSTALL Step 3: Configure remote-mode ---${NC}"
    # Create remote-mode marker file
    touch "${KITTYHACK_ROOT}/.remote-mode"

    # Configure config.ini (best-effort): camera source on remote nodes
    if [ -f "${KITTYHACK_ROOT}/config.ini" ]; then
        sed -i "s/^camera_source\s*=\s*.*/camera_source = ip_camera/" "${KITTYHACK_ROOT}/config.ini" || true
    fi

    echo -e "${CYAN}--- REMOTE MODE INSTALL Step 4: Install/enable kittyhack.service ---${NC}"
    cat << EOF > /etc/systemd/system/kittyhack.service
[Unit]
Description=KittyHack WebGUI
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=${KITTYHACK_ROOT}
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${KITTYHACK_ROOT}/.venv/bin"
ExecStart=${KITTYHACK_ROOT}/.venv/bin/shiny run --host=0.0.0.0 --port=80
Restart=always
RestartSec=5

KillSignal=SIGTERM
KillMode=mixed
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable kittyhack.service
    systemctl restart kittyhack.service

    echo -e "${GREEN}Remote-mode installation complete.${NC}"
}

# Update Kittyhack only
install_update() {
    echo -e "${CYAN}--- Check internet connection ---${NC}"
    if ! ping -c 1 google.com &>/dev/null; then
        if $USE_LOCAL_SOURCES; then
            echo -e "${YELLOW}No internet connection detected. Continuing because --use-local-sources was set.${NC}"
        else
            echo -e "${RED}No internet connection detected. Please check your connection and try again.${NC}"
            exit 1
        fi
    else
        echo -e "${GREEN}Internet connection is active.${NC}"
    fi

    echo -e "${CYAN}--- Stop kittyhack service ---${NC}"
    if systemctl is-active --quiet kittyhack.service; then
        systemctl stop kittyhack.service
        if systemctl is-active --quiet kittyhack.service; then
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to stop KittyHack service.${NC}"
        else
            echo -e "${GREEN}KittyHack service stopped successfully.${NC}"
        fi
    else
            echo -e "${GREY}KittyHack service is already inactive.${NC}"
    fi

    install_kittyhack
}

install_kittyhack() {
    if $USE_LOCAL_SOURCES; then
        KITTYHACK_INSTALL_DIR="$KITTYHACK_ROOT"
        echo -e "${CYAN}--- KITTYHACK INSTALL Step 1: Use local sources ---${NC}"
        echo -e "${GREY}Using local sources from: ${KITTYHACK_INSTALL_DIR}${NC}"

        if [[ ! -f "${KITTYHACK_INSTALL_DIR}/requirements.txt" ]]; then
            ((FAIL_COUNT++))
            echo -e "${RED}Local sources appear invalid: requirements.txt not found in ${KITTYHACK_INSTALL_DIR}.${NC}"
            return 1
        fi

        # Best-effort: if an old /root/kittyhack exists, import its config/db if local ones don't exist.
        if [[ -d /root/kittyhack && "${KITTYHACK_INSTALL_DIR}" != "/root/kittyhack" ]]; then
            [ -f /root/kittyhack/config.ini ] && [ ! -f "${KITTYHACK_INSTALL_DIR}/config.ini" ] && cp /root/kittyhack/config.ini "${KITTYHACK_INSTALL_DIR}/config.ini" || true
            [ -f /root/kittyhack/config.remote.ini ] && [ ! -f "${KITTYHACK_INSTALL_DIR}/config.remote.ini" ] && cp /root/kittyhack/config.remote.ini "${KITTYHACK_INSTALL_DIR}/config.remote.ini" || true
            [ -f /root/kittyhack/kittyhack.db ] && [ ! -f "${KITTYHACK_INSTALL_DIR}/kittyhack.db" ] && cp /root/kittyhack/kittyhack.db "${KITTYHACK_INSTALL_DIR}/kittyhack.db" || true
        fi
    else
        KITTYHACK_INSTALL_DIR="/root/kittyhack"
        echo -e "${CYAN}--- KITTYHACK INSTALL Step 1: Clone KittyHack repository ---${NC}"
        if [[ -d "${KITTYHACK_INSTALL_DIR}" ]]; then
            echo -e "${GREY}Existing KittyHack repository found. Backing up database and config.ini...${NC}"

            # Backup important files if they exist
            [ -f "${KITTYHACK_INSTALL_DIR}/config.ini" ] && cp "${KITTYHACK_INSTALL_DIR}/config.ini" /tmp/config.ini.bak
            [ -f "${KITTYHACK_INSTALL_DIR}/config.remote.ini" ] && cp "${KITTYHACK_INSTALL_DIR}/config.remote.ini" /tmp/config.remote.ini.bak
            [ -f "${KITTYHACK_INSTALL_DIR}/kittyhack.db" ] && cp "${KITTYHACK_INSTALL_DIR}/kittyhack.db" /tmp/kittyhack.db.bak

            # Remove old repository
            echo -e "${GREY}removing kittyhack installation...${NC}"
            rm -rf "${KITTYHACK_INSTALL_DIR}"
        fi

        echo -e "${GREY}Cloning KittyHack repository...${NC}"
        git clone https://github.com/floppyFK/kittyhack.git "${KITTYHACK_INSTALL_DIR}" --quiet
        if [[ $? -eq 0 ]]; then
            echo -e "${GREEN}Repository cloned successfully.${NC}"
        else
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to clone the repository. Please check your internet connection.${NC}"
        fi
    fi

    if $USE_LOCAL_SOURCES; then
        GIT_TAG=""
        if [[ -n "$KITTYHACK_VERSION_REF" ]]; then
            echo -e "${YELLOW}--version was set to '${KITTYHACK_VERSION_REF}', but local sources are used; skipping git checkout.${NC}"
        else
            echo -e "${GREY}Skipping release checkout because local sources are used.${NC}"
        fi
    elif [[ -n "$KITTYHACK_VERSION_REF" ]]; then
        GIT_TAG="$KITTYHACK_VERSION_REF"
        echo -e "${GREY}Using requested version ref: ${GIT_TAG}${NC}"
    elif [ $INSTALL_LEGACY_KITTYHACK -eq 0 ]; then
        echo -e "${GREY}Fetching latest release...${NC}"
        # Try up to 5 times to get the latest tag
        for i in {1..5}; do
            GIT_TAG=$(curl -sf https://api.github.com/repos/floppyFK/kittyhack/releases/latest | grep -Po '"tag_name": "\K.*?(?=")')
            if [[ -n "$GIT_TAG" ]]; then
                break
            fi
            # Alternative method using git ls-remote
            GIT_TAG=$(git ls-remote --tags --refs https://github.com/floppyFK/kittyhack.git | tail -n1 | sed 's/.*\///')
            if [[ -n "$GIT_TAG" ]]; then
                break
            fi
            echo -e "${YELLOW}Attempt $i of 5 to fetch latest release failed. Retrying in 10 seconds...${NC}"
            sleep 10
        done

        # If still empty, use fallback version
        if [[ -z "$GIT_TAG" ]]; then
            echo -e "${YELLOW}Failed to fetch latest version. Using latest known version.${NC}"
            GIT_TAG="v2.4.0"
        fi
    else
        GIT_TAG="v1.1.1"
    fi

    if [[ -n "$GIT_TAG" && $USE_LOCAL_SOURCES == false ]]; then
        echo -e "${GREY}Checking out ${GIT_TAG}...${NC}"
        git -C "${KITTYHACK_INSTALL_DIR}" fetch --all --tags --quiet || true
        git -C "${KITTYHACK_INSTALL_DIR}" checkout "${GIT_TAG}" --quiet
        if [[ $? -eq 0 ]]; then
            echo -e "${GREEN}Repository updated to ${GIT_TAG}${NC}"
        else
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to checkout ${GIT_TAG}. Please check your internet connection.${NC}"
        fi
    elif [[ $USE_LOCAL_SOURCES == false ]]; then
        ((FAIL_COUNT++))
        echo -e "${RED}Failed to fetch tags. Please check your internet connection.${NC}"
    fi
    
    # Restore backed up files if they exist
    if [ -f /tmp/config.ini.bak ]; then
        cp /tmp/config.ini.bak "${KITTYHACK_INSTALL_DIR}/config.ini" && rm -f /tmp/config.ini.bak
    fi
    if [ -f /tmp/config.remote.ini.bak ]; then
        cp /tmp/config.remote.ini.bak "${KITTYHACK_INSTALL_DIR}/config.remote.ini" && rm -f /tmp/config.remote.ini.bak
    fi
    if [ -f /tmp/kittyhack.db.bak ]; then
        cp /tmp/kittyhack.db.bak "${KITTYHACK_INSTALL_DIR}/kittyhack.db" && rm -f /tmp/kittyhack.db.bak
    fi

    echo -e "${CYAN}--- KITTYHACK INSTALL Step 2: Set up Python virtual environment ---${NC}"
    if ! create_venv_py311 "${KITTYHACK_INSTALL_DIR}/.venv"; then
        ((FAIL_COUNT++))
        echo -e "${RED}Failed to create Python 3.11 virtualenv.${NC}"
        return 1
    fi
    source "${KITTYHACK_INSTALL_DIR}/.venv/bin/activate"

    # Force pip to use PyPI only to satisfy --require-hashes entries in requirements.txt
    # Some systems have PIP_EXTRA_INDEX_URL=piwheels; unset it to avoid hash mismatches.
    unset PIP_EXTRA_INDEX_URL
    export PIP_INDEX_URL="https://pypi.org/simple"

    # Upgrade pip/setuptools/wheel to improve compatibility
    pip install --timeout 120 --no-cache-dir -U pip setuptools wheel

    # Install project dependencies from PyPI (no extra indexes), fail fast if hashes don’t match
    if ! pip install --timeout 120 --no-cache-dir -r "${KITTYHACK_INSTALL_DIR}/requirements.txt"; then
        ((FAIL_COUNT++))
        echo -e "${RED}Failed to install Python dependencies.${NC}"
    else
        echo -e "${GREEN}Python dependencies installed/updated.${NC}"
    fi
    deactivate

    echo -e "${CYAN}--- KITTYHACK INSTALL Step 3: Start kwork process ---${NC}"
    if [ $INSTALL_LEGACY_KITTYHACK -eq 1 ]; then
        # Installation of kittyhack v1.1.1 - we need to start the kwork service
        write_kwork_service
        
        # Rename the kwork executable if it was renamed to main_disabled (leave the manager and dependencies renamed)
        patterns=(
            "/root/kittyflap_versions/*/main"
        )
        for pattern in "${patterns[@]}"; do
            for path in $(find /root/kittyflap_versions -type f -name "$(basename $pattern)_disabled"); do
                if [ -f "$path" ]; then
                    mv "$path" "${path%_disabled}"
                    echo -e "${GREEN}$(basename $path) executable renamed back to original.${NC}"
                else
                    echo -e "${GREY}$(basename $path) executable not found. Skipping.${NC}"
                fi
            done
        done

        enable_service "kwork"
        # check if kwork is running
        if is_service_active "kwork"; then
            echo -e "${GREEN}Kwork service started successfully.${NC}"
        else
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to start kwork service.${NC}"
        fi
    else
        echo -e "${GREY}Skipping kwork service start for the latest version.${NC}"
    fi


    echo -e "${CYAN}--- KITTYHACK INSTALL Step 4: Install KittyHack service (supervised by kittyhack_control) ---${NC}"
    cat > /etc/systemd/system/kittyhack.service << EOF
[Unit]
Description=KittyHack WebGUI
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=${KITTYHACK_INSTALL_DIR}
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${KITTYHACK_INSTALL_DIR}/.venv/bin"
ExecStart=${KITTYHACK_INSTALL_DIR}/.venv/bin/shiny run --host=0.0.0.0 --port=80
Restart=always
RestartSec=5

KillSignal=SIGTERM
KillMode=mixed
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    # Target-mode: kittyhack_control supervises kittyhack startup. Do not enable kittyhack.service here.
    systemctl disable kittyhack.service 2>/dev/null || true
    systemctl stop kittyhack.service 2>/dev/null || true
    echo -e "${GREEN}KittyHack service installed (will be started by kittyhack_control).${NC}"

    echo -e "${CYAN}--- KITTYHACK INSTALL Step 5: Install and start kittyhack_control service ---${NC}"
    cat > /etc/systemd/system/kittyhack_control.service << EOF
[Unit]
Description=KittyHack Control Service (remote control target)
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=${KITTYHACK_INSTALL_DIR}
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${KITTYHACK_INSTALL_DIR}/.venv/bin"
ExecStart=${KITTYHACK_INSTALL_DIR}/.venv/bin/python -m src.kittyhack_control
Restart=always
RestartSec=2

KillSignal=SIGTERM
KillMode=mixed
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable kittyhack_control.service
    systemctl start kittyhack_control.service
    if systemctl is-active --quiet kittyhack_control.service; then
        echo -e "${GREEN}kittyhack_control service installed and started successfully.${NC}"
    else
        echo -e "${RED}Failed to start kittyhack_control service.${NC}"
    fi

    # Log system journal information
    for i in {1..5}; do
        echo -n "."
        sleep 1
    done
    echo # move to the next line after the loop
    journalctl -n 20000 > "${LOGPATH}/journal.log"
    journalctl -n 1000 -u kwork > "${LOGPATH}/journal_kwork.log"
    dpkg-query -W > "${LOGPATH}/installed_packages_after_kittyhack_setup.log"
    systemctl list-units --type=service --all > "${LOGPATH}/systemd-services_after_kittyhack_setup.log"
}

uninstall_kittyhack() {
    local REMOVE_DIR="/root/kittyhack"
    local wd=""

    # Best-effort: detect install directory from unit before removing it
    if command -v systemctl >/dev/null 2>&1; then
        wd=$(systemctl show -p WorkingDirectory --value kittyhack.service 2>/dev/null || true)
    fi
    if [[ -z "$wd" && -f /etc/systemd/system/kittyhack.service ]]; then
        wd=$(grep -E '^WorkingDirectory=' /etc/systemd/system/kittyhack.service | head -n1 | cut -d= -f2-)
    fi
    if [[ -n "$wd" ]]; then
        REMOVE_DIR="$wd"
    fi

    echo -e "${CYAN}--- UNINSTALL: Stop and disable services ---${NC}"
    systemctl stop kittyhack_control.service 2>/dev/null || true
    systemctl stop kittyhack.service 2>/dev/null || true
    systemctl stop kwork.service 2>/dev/null || true

    systemctl disable kittyhack_control.service 2>/dev/null || true
    systemctl disable kittyhack.service 2>/dev/null || true
    systemctl disable kwork.service 2>/dev/null || true

    echo -e "${CYAN}--- UNINSTALL: Remove systemd unit files ---${NC}"
    rm -f /etc/systemd/system/kittyhack_control.service
    rm -f /etc/systemd/system/kittyhack.service
    rm -f /etc/systemd/system/kwork.service
    systemctl daemon-reload

    echo -e "${CYAN}--- UNINSTALL: Remove files ---${NC}"
    if [[ -d "$REMOVE_DIR" ]]; then
        if [[ "$REMOVE_DIR" == "/" || "$REMOVE_DIR" == "/root" || -z "$REMOVE_DIR" ]]; then
            echo -e "${YELLOW}Refusing to remove suspicious directory: '$REMOVE_DIR'.${NC}"
        elif [[ "$REMOVE_DIR" != "/root/kittyhack" ]]; then
            echo -e "${YELLOW}Detected install directory: ${REMOVE_DIR}${NC}"
            read -r -p "Remove this directory as well? (y/N): " CONFIRM_RM
            case "${CONFIRM_RM,,}" in
                y|yes|j|ja)
                    rm -rf "$REMOVE_DIR"
                    ;;
                *)
                    echo -e "${GREY}Keeping ${REMOVE_DIR}.${NC}"
                    ;;
            esac
        else
            rm -rf "$REMOVE_DIR"
        fi
    else
        echo -e "${GREY}Install directory not found (${REMOVE_DIR}). Skipping file removal.${NC}"
    fi

    # Optional: remove trained models and saved pictures (common locations on the target)
    for extra_dir in "/root/models" "/root/pictures"; do
        if [[ -d "$extra_dir" ]]; then
            local prompt
            if [ "$LANGUAGE" == "de" ]; then
                if [[ "$extra_dir" == "/root/models" ]]; then
                    prompt="Ordner mit trainierten YOLO Modellen entfernen (${extra_dir})? (y/N): "
                else
                    prompt="Ordner mit gespeicherten Bildern entfernen (${extra_dir})? (y/N): "
                fi
            else
                if [[ "$extra_dir" == "/root/models" ]]; then
                    prompt="Remove trained YOLO models folder (${extra_dir})? (y/N): "
                else
                    prompt="Remove saved pictures folder (${extra_dir})? (y/N): "
                fi
            fi

            read -r -p "$prompt" CONFIRM_EXTRA
            case "${CONFIRM_EXTRA,,}" in
                y|yes|j|ja)
                    if [[ "$extra_dir" == "/" || "$extra_dir" == "/root" || -z "$extra_dir" ]]; then
                        echo -e "${YELLOW}Refusing to remove suspicious directory: '$extra_dir'.${NC}"
                    else
                        rm -rf "$extra_dir"
                    fi
                    ;;
                *)
                    echo -e "${GREY}Keeping ${extra_dir}.${NC}"
                    ;;
            esac
        fi
    done

    echo -e "${GREEN}Uninstall complete.${NC}"
}

reinstall_camera_drivers() {
    echo -e "${CYAN}--- CAMERA DRIVER REINSTALL ---${NC}"
    # Download and install camera drivers
    local base_url="https://github.com/floppyFK/kittyhack-dependencies/raw/refs/heads/main/camera"
    local dependencies_file="${KITTYHACK_INSTALL_DIR}/camera_dependencies.txt"
    local download_dir="/tmp/camera_drivers"

    mkdir -p "$download_dir"

    if [[ -f "$dependencies_file" ]]; then
        while IFS= read -r package; do
            echo -e "${GREY}Downloading $package...${NC}"
            curl -L -o "${download_dir}/${package}" "${base_url}/${package}"
            if [[ $? -ne 0 ]]; then
                ((FAIL_COUNT++))
                echo -e "${RED}Failed to download $package.${NC}"
                continue
            fi
        done < "$dependencies_file"
    else
        echo -e "${RED}Dependencies file $dependencies_file not found. Please report this in the GitHub repository.${NC}"
        return 1
    fi

    echo -e "${GREY}Installing downloaded packages...${NC}"
    dpkg -i ${download_dir}/*.deb
    if [[ $? -ne 0 ]]; then
        ((FAIL_COUNT++))
        echo -e "${RED}Failed to install some packages.${NC}"
    else
        echo -e "${GREEN}All packages installed successfully.${NC}"
    fi

    rm -rf "$download_dir"
}

# Main script logic

# Defaults / CLI flags
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
KITTYHACK_ROOT=$(realpath "${SCRIPT_DIR}/..")

# When true: do not clone from GitHub, use this repo folder as install source.
USE_LOCAL_SOURCES=false

# The directory where kittyhack will run from (venv, config.ini, kittyhack.db).
# Default is /root/kittyhack for backward compatibility.
KITTYHACK_INSTALL_DIR="/root/kittyhack"

print_usage() {
    cat << EOF
Usage: $(basename "$0") [en|de] [--use-local-sources] [--sources-dir=/path] [--lang=en|de] [--version=<tag-or-commit>] [--uninstall]

  --use-local-sources   Use the current repo folder (no git clone).
  --sources-dir=/path   Use a specific local repo folder (implies --use-local-sources).
  --lang=en|de          Set UI language.
    --version=<ref>       Install a specific git ref (tag/commit) instead of the latest release tag.
    --uninstall           Uninstall kittyhack (stops/removes systemd services; optionally removes files).
EOF
}

# Parse args (order independent)
LANGUAGE="en"
for arg in "$@"; do
    case "$arg" in
        --uninstall)
            DO_UNINSTALL=true
            ;;
        --use-local-sources)
            USE_LOCAL_SOURCES=true
            ;;
        --sources-dir=*)
            USE_LOCAL_SOURCES=true
            KITTYHACK_ROOT=$(realpath "${arg#*=}")
            ;;
        --version=*)
            KITTYHACK_VERSION_REF="${arg#*=}"
            ;;
        --lang=*)
            LANGUAGE="${arg#*=}"
            ;;
        en|de)
            LANGUAGE="$arg"
            ;;
        -h|--help)
            print_usage
            exit 0
            ;;
    esac
done

if $DO_UNINSTALL; then
    uninstall_kittyhack
    exit 0
fi

# If local sources are used, default install dir to that folder.
if $USE_LOCAL_SOURCES; then
    KITTYHACK_INSTALL_DIR="$KITTYHACK_ROOT"
fi

ensure_uv_installed() {
    if command -v uv >/dev/null 2>&1; then
        return 0
    fi

    echo -e "${CYAN}Installing uv (Python version manager) ...${NC}"
    apt-get update -y >/dev/null 2>&1 || true
    apt-get install -y curl ca-certificates >/dev/null 2>&1 || true

    # Official installer (installs into ~/.local/bin)
    if curl -LsSf https://astral.sh/uv/install.sh | sh; then
        export PATH="$PATH:/root/.local/bin"
    fi

    if ! command -v uv >/dev/null 2>&1; then
        echo -e "${RED}Failed to install uv. Please install it manually (https://astral.sh/uv) or install python3.11 via apt.${NC}"
        return 1
    fi
}

ensure_python311_available() {
    if command -v python3.11 >/dev/null 2>&1; then
        return 0
    fi

    echo -e "${YELLOW}python3.11 not found. Trying to install python3.11 via apt...${NC}"
    apt-get update -y >/dev/null 2>&1 || true
    if apt-get install -y python3.11 python3.11-venv python3.11-dev >/dev/null 2>&1; then
        if command -v python3.11 >/dev/null 2>&1; then
            return 0
        fi
    fi

    echo -e "${YELLOW}python3.11 not available via apt. Falling back to uv-managed Python 3.11...${NC}"
    ensure_uv_installed || return 1
    uv python install 3.11 || return 1
    return 0
}

create_venv_py311() {
    local venv_path="$1"

    # Ensure we don't keep an old venv created with a different Python version.
    if [[ -d "$venv_path" ]]; then
        rm -rf "$venv_path"
    fi

    ensure_python311_available || return 1

    if command -v python3.11 >/dev/null 2>&1; then
        python3.11 -m venv "$venv_path"
        return $?
    fi

    uv venv --python 3.11 "$venv_path"
}

# Environment override (kept for compatibility)
if [ -n "$SCRIPT_LANG" ]; then
    LANGUAGE="$SCRIPT_LANG"
fi

# Check for command-line arguments or prompt for a choice 
ERRMSG=""

while true; do
    # Clear the terminal
    clear

    # ASCII art
cat << "EOF"
 _   __ _  _    _           _   _               _    
| | / /(_)| |  | |         | | | |             | |   
| |/ /  _ | |_ | |_  _   _ | |_| |  __ _   ___ | | __
|    \ | || __|| __|| | | ||  _  | / _` | / __|| |/ /
| |\  \| || |_ | |_ | |_| || | | || (_| || (__ |   < 
\_| \_/|_| \__| \__| \__, |\_| |_/ \__,_| \___||_|\_\
                      __/ |                          
                     |___/                           
EOF

    # Menu
        free_space=$(df -h --output=avail / | awk 'NR==2 {
        val = $1
        # Add "B" if not already present
        if (val !~ /B$/) val = val "B"
        len = length(val)
        total = 75
        pad = int((total - len) / 2)
        printf "%*s%s%*s", pad, "", val, total - len - pad, ""
    }')
    echo
    if [ "$LANGUAGE" == "de" ]; then
        echo -e "Willkommen zum KittyHack-Setup!"
        echo
        echo -e "+--------------------------------- ${CYAN}HINWEIS${NC} ---------------------------------+"
        echo -e "| Bitte stelle vor der Installation sicher, dass genügend freier Speicher-  |"
        echo -e "| platz vorhanden ist. Für die Installation werden mindestens 2GB freier    |"
        echo -e "| Speicherplatz benötigt! Aktuell verfügbar:                                |"
        echo -e "|                                                                           |"
        echo -e "|${CYAN}${free_space}${NC}|"
        echo -e "|                                                                           |"
        echo -e "+---------------------------------------------------------------------------+"
    else
        echo -e "Welcome to the KittyHack setup!"
        echo
        echo -e "+---------------------------------- ${CYAN}NOTE${NC} -----------------------------------+"
        echo -e "| Please ensure that you have enough free disk space before installation.   |"
        echo -e "| The installation requires at least 2GB of free disk space!                |"
        echo -e "| Currently available:                                                      |"
        echo -e "|                                                                           |"
        echo -e "|${CYAN}${free_space}${NC}|"
        echo -e "|                                                                           |"
        echo -e "+---------------------------------------------------------------------------+"
    fi
    
    echo -e "${ERRMSG}" 
    if [ "$LANGUAGE" == "de" ]; then
        echo -e "${CYAN}Bitte die gewünschte Option auswählen:${NC}"
        echo -e "${GREY}--- Zielgerät (direkt auf einer Kittyflap) ---${NC}"
        echo -e "(${BLUE}${FMTBOLD}1${FMTDEF}${NC}) Erstmalige Installation von Kittyhack ausführen"
        echo -e "(${BLUE}${FMTBOLD}2${FMTDEF}${NC}) Kameratreiber erneut installieren (bitte nur ausführen, wenn du keine Live-Bilder siehst. Ist inzwischen auch über die Kittyhack Web-Oberfläche möglich)"
        echo -e "(${BLUE}${FMTBOLD}3${FMTDEF}${NC}) Update auf die neueste Version von Kittyhack (bitte nur ausführen, wenn du bereits Kittyhack installiert hast)"
        echo -e ""
        echo -e "${GREY}--- Remote-Mode (beliebiger Linux-PC) ---${NC}"
        echo -e "(${BLUE}${FMTBOLD}4${FMTDEF}${NC}) Installation als Remote-Control (remote-mode) auf einem separaten System"
        echo -e ""
        echo -e "${GREY}--- Sonstiges ---${NC}"
        echo -e "(${BLUE}${FMTBOLD}5${FMTDEF}${NC}) Kittyhack deinstallieren"
        echo -e "(${BLUE}${FMTBOLD}b${FMTDEF}${NC})eenden"
    else
        echo -e "${CYAN}Please choose the desired option:${NC}"
        echo -e "${GREY}--- Target device (directly on a Kittyflap) ---${NC}"
        echo -e "(${BLUE}${FMTBOLD}1${FMTDEF}${NC}) Run initial installation of Kittyhack"
        echo -e "(${BLUE}${FMTBOLD}2${FMTDEF}${NC}) Reinstall camera drivers (only run if you don't see live images. Can also be done via the Kittyhack web interface now)"
        echo -e "(${BLUE}${FMTBOLD}3${FMTDEF}${NC}) Update to the latest version of Kittyhack (only run if you already have Kittyhack installed)"
        echo -e ""
        echo -e "${GREY}--- Remote mode (any Linux PC) ---${NC}"
        echo -e "(${BLUE}${FMTBOLD}4${FMTDEF}${NC}) Install as remote-control (remote-mode) on a dedicated system"
        echo -e ""
        echo -e "${GREY}--- Other ---${NC}"
        echo -e "(${BLUE}${FMTBOLD}5${FMTDEF}${NC}) Uninstall Kittyhack"
        echo -e "(${BLUE}${FMTBOLD}q${FMTDEF}${NC})uit"
    fi
    read -r MODE

    # Handle the input
    case "$MODE" in
        1)
            echo -e "${CYAN}Installing the latest version of Kittyhack...${NC}"
            INSTALL_LEGACY_KITTYHACK=0
            install_full
            break
            ;;
        2)
            echo -e "${CYAN}Reinstalling camera drivers...${NC}"
            reinstall_camera_drivers
            break
            ;;
        3)
            echo -e "${CYAN}Updating to the latest version of Kittyhack...${NC}"
            INSTALL_LEGACY_KITTYHACK=0
            install_update
            break
            ;;
        4)
            echo -e "${CYAN}Installing Kittyhack in remote-mode...${NC}"
            INSTALL_LEGACY_KITTYHACK=0
            install_remote_mode
            break
            ;;
        5)
            echo -e "${CYAN}Uninstalling Kittyhack...${NC}"
            uninstall_kittyhack
            exit 0
            ;;
        q|b)
            echo -e "${YELLOW}Quitting installation.${NC}"
            exit 0
            ;;
        *)
            if [ "$LANGUAGE" == "de" ]; then
                ERRMSG="${RED}Ungültige Eingabe. Bitte '1', '2', '3', '4', '5' oder 'b' eingeben.${NC}\n"
            else
                ERRMSG="${RED}Invalid choice. Please enter '1', '2', '3', '4', '5' or 'q'.${NC}\n"
            fi
            MODE=""
            ;;
    esac
done

if systemctl is-active --quiet kittyhack.service; then
    if [ $FAIL_COUNT -eq 0 ]; then
        if [ "$LANGUAGE" == "de" ]; then
            echo -e "\n${GREEN}Setup abgeschlossen!${NC}\n"
            echo -e "Öffne ${CYAN}http://${CURRENT_IP}${NC} in deinem Browser, um auf deine Kittyflap zuzugreifen!\n"
        else
            echo -e "\n${GREEN}Setup complete!${NC}\n"
            echo -e "Open ${CYAN}http://${CURRENT_IP}${NC} in your browser to access your Kittyflap!\n"
        fi
    else
        if [ "$LANGUAGE" == "de" ]; then
            echo -e "\n${YELLOW}Setup mit Warnungen abgeschlossen. Bitte überprüfe die Protokolle in ${LOGFILE} für weitere Details.${NC}\n"
            echo -e "Du könntest trotzdem versuchen, ${CYAN}http://${CURRENT_IP}${NC} in deinem Browser zu öffnen, um auf deine Kittyflap zuzugreifen.\n"
        else
            echo -e "\n${YELLOW}Setup complete with warnings. Please check the logs in ${LOGFILE} for more details.${NC}\n"
            echo -e "You could still try to open ${CYAN}http://${CURRENT_IP}${NC} in your browser to access your Kittyflap.\n"
        fi
    fi
else
    if [ "$LANGUAGE" == "de" ]; then
        echo -e "\n${RED}Setup fehlgeschlagen. Bitte überprüfe die Protokolle in ${LOGFILE} für weitere Details.${NC}\n"
    else
        echo -e "\n${RED}Setup failed. Please check the logs in ${LOGFILE} for more details.${NC}\n"
    fi
fi
