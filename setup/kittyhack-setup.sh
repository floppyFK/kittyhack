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

# Highest stable release tag (vX.Y.Z). Beta tags such as v3.0.0_beta_2 are ignored.
# Reads tag names or `git ls-remote` lines from stdin.
pick_latest_stable_tag() {
    sed 's#.*refs/tags/##' \
        | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
        | sort -V \
        | tail -n1
}

# Latest GitHub release, then the highest stable tag if the API stays empty.
# Prints the tag on stdout. Warnings go to stderr so command substitution stays clean.
# --version may still select a beta ref; this helper is only the default.
fetch_latest_stable_release_tag() {
    local tag=""
    local attempt
    for attempt in 1 2 3 4 5; do
        tag=$(curl -sf "https://api.github.com/repos/floppyFK/kittyhack/releases/latest" | grep -Po '"tag_name": "\K.*?(?=")' || true)
        if [[ -n "$tag" ]]; then
            printf '%s\n' "$tag"
            return 0
        fi
        if [[ "$attempt" -lt 5 ]]; then
            echo -e "${YELLOW}Attempt ${attempt} of 5 to fetch latest release failed. Retrying in 10 seconds...${NC}" >&2
            sleep 10
        fi
    done

    echo -e "${YELLOW}GitHub latest-release API unavailable. Selecting the highest stable tag...${NC}" >&2
    tag=$(
        git ls-remote --tags --refs "https://github.com/floppyFK/kittyhack.git" \
            | pick_latest_stable_tag || true
    )
    if [[ -n "$tag" ]]; then
        printf '%s\n' "$tag"
        return 0
    fi
    return 1
}

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

upsert_db_setting() {
    local db="$1"
    local key="$2"
    local json_value="$3"
    python3 - "$db" "$key" "$json_value" << 'PY'
import sqlite3
import sys
from datetime import datetime, timezone

db, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
conn = sqlite3.connect(db, timeout=30, isolation_level=None)
conn.execute("PRAGMA busy_timeout = 30000")
conn.execute(
    """CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )"""
)
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
conn.execute("BEGIN IMMEDIATE")
conn.execute(
    "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
    (key, value, now),
)
conn.execute("COMMIT")
conn.close()
PY
}

set_config_language() {
    local install_dir="$1"
    local config_file="${install_dir}/config.ini"
    local lang="en"

    if [[ "$LANGUAGE" == "de" ]]; then
        lang="de"
    fi

    if upsert_db_setting "${install_dir}/kittyhack.db" "LANGUAGE" "\"${lang}\""; then
        echo -e "${GREEN}Stored setup language in kittyhack.db: language = ${lang}${NC}"
    else
        ((FAIL_COUNT++))
        echo -e "${RED}Failed to store language in kittyhack.db.${NC}"
    fi

    # A leftover config.ini is imported on the next start and would overwrite the
    # database row. Keep the language in that file until the import deletes it.
    if [[ ! -f "$config_file" ]]; then
        return 0
    fi

    awk -v new_lang="$lang" '
        BEGIN { in_settings=0; language_set=0; saw_settings=0 }

        /^\[Settings\][[:space:]]*$/ {
            in_settings=1
            saw_settings=1
            print
            next
        }

        in_settings && /^\[/ {
            if (!language_set) {
                print "language = " new_lang
                language_set=1
            }
            in_settings=0
        }

        in_settings && /^[[:space:]]*language[[:space:]]*=/ {
            print "language = " new_lang
            language_set=1
            next
        }

        { print }

        END {
            if (!saw_settings) {
                print ""
                print "[Settings]"
                print "language = " new_lang
            } else if (in_settings && !language_set) {
                print "language = " new_lang
            }
        }
    ' "$config_file" > "${config_file}.tmp" && mv "${config_file}.tmp" "$config_file"

    if [[ $? -eq 0 ]]; then
        echo -e "${GREEN}Configured setup language in config.ini: language = ${lang}${NC}"
    else
        ((FAIL_COUNT++))
        echo -e "${RED}Failed to update language in ${config_file}.${NC}"
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
    INSTALL_CAMERA_DRIVERS=true
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

    # The libcamera GStreamer plugin is installed later from the pinned debs in
    # camera_dependencies.txt. The archive build depends on libcamera 0.5, which
    # the Kittyflap apt pin on libcamera-ipa 0.2.0 will not allow.
    GSTREAMER_PACKAGES=(
        gstreamer1.0-tools
        gstreamer1.0-plugins-base
        gstreamer1.0-plugins-good
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
        if dpkg-query -W -f '${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
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
        gettext
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
        echo -e "  - Mindestens 4GB RAM"
        echo -e "Folgende Pakete werden installiert:"
        echo -e "  - python3, python3-venv, python3-pip, gettext"
        echo -e "  - rsync, git, curl, ca-certificates"
        echo -e "  - libgl1, libglib2.0-0"
        echo -e "Zusatzlich: Es wird ein Python 3.14 Virtualenv erstellt und die Abhangigkeiten aus requirements_remote.txt installiert."
        read -r -p "Mit diesen Installationen fortfahren? (y/N): " CONFIRM_REMOTE_MODE
    else
        echo -e "NOTE: The installation requires root rights. It is strongly recommended to install it on an separate system, which acts only as a kittyhack remote controller."
        echo -e ""
        echo -e "System requirements:"
        echo -e "  - Debian (or derivative, e.g. Ubuntu)"
        echo -e "  - Ideally a clean system or VM"
        echo -e "  - AMD64 (regular 64-bit Intel/AMD PCs, not Raspberry Pi/ARM)"
        echo -e "  - At least 4GB RAM"
        echo -e "The following packages will be installed:"
        echo -e "  - python3, python3-venv, python3-pip, gettext"
        echo -e "  - rsync, git, curl, ca-certificates"
        echo -e "  - libgl1, libglib2.0-0"
        echo -e "Additionally: A Python 3.14 virtualenv will be created and dependencies from requirements_remote.txt will be installed."
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

    # Determine source/install directory for remote mode.
    if $USE_LOCAL_SOURCES; then
        if [[ -z "$KITTYHACK_ROOT" || "$KITTYHACK_ROOT" == "/" ]]; then
            echo -e "${RED}Invalid local source directory: '${KITTYHACK_ROOT}'. Use --sources-dir=/path/to/kittyhack.${NC}"
            exit 1
        fi
        KITTYHACK_INSTALL_DIR="$KITTYHACK_ROOT"
        if [[ ! -f "${KITTYHACK_INSTALL_DIR}/requirements_remote.txt" ]]; then
            echo -e "${RED}Local sources appear invalid: requirements_remote.txt not found in ${KITTYHACK_INSTALL_DIR}.${NC}"
            exit 1
        fi
    else
        KITTYHACK_INSTALL_DIR="/root/kittyhack"
        echo -e "${CYAN}--- REMOTE MODE INSTALL: Clone KittyHack repository ---${NC}"
        if [[ -d "${KITTYHACK_INSTALL_DIR}" ]]; then
            rm -rf "${KITTYHACK_INSTALL_DIR}"
        fi
        if ! git clone https://github.com/floppyFK/kittyhack.git "${KITTYHACK_INSTALL_DIR}" --quiet; then
            echo -e "${RED}Failed to clone the repository. Please check your internet connection.${NC}"
            exit 1
        fi

        local remote_git_ref=""
        if [[ -n "$KITTYHACK_VERSION_REF" ]]; then
            remote_git_ref="$KITTYHACK_VERSION_REF"
            echo -e "${GREY}Using requested version ref: ${remote_git_ref}${NC}"
        else
            echo -e "${GREY}Fetching latest release...${NC}"
            remote_git_ref=$(fetch_latest_stable_release_tag || true)
            if [[ -z "$remote_git_ref" ]]; then
                remote_git_ref="v2.4.0"
                echo -e "${YELLOW}Failed to fetch latest version. Using fallback ${remote_git_ref}.${NC}"
            fi
        fi

        if [[ -n "$remote_git_ref" ]]; then
            if ! git -C "${KITTYHACK_INSTALL_DIR}" checkout "$remote_git_ref" --quiet; then
                echo -e "${RED}Failed to checkout ${remote_git_ref}.${NC}"
                exit 1
            fi
        fi
    fi
    echo -e "${CYAN}Kittyhack path: ${KITTYHACK_INSTALL_DIR}${NC}"

    echo -e "${CYAN}--- REMOTE MODE INSTALL Step 1: Install system packages ---${NC}"
    if ! apt-get update; then
        echo -e "${RED}Failed to update apt package lists.${NC}"
        exit 1
    fi
    # libgl1 is required by some OpenCV wheels (even in headless/container setups)
    if ! apt-get install -y python3 python3-venv python3-pip gettext rsync git curl ca-certificates libgl1 libglib2.0-0; then
        echo -e "${RED}Failed to install required system packages.${NC}"
        exit 1
    fi

    echo -e "${CYAN}--- REMOTE MODE INSTALL Step 2: Create virtualenv + install Python deps ---${NC}"
    cd "$KITTYHACK_INSTALL_DIR" || exit 1
    if ! bash "${KITTYHACK_INSTALL_DIR}/setup/ensure_venv.sh" \
        --bootstrap \
        --root "${KITTYHACK_INSTALL_DIR}" \
        --requirements "${KITTYHACK_INSTALL_DIR}/requirements_remote.txt"; then
        echo -e "${RED}Failed to create Python virtualenv / install dependencies. Aborting.${NC}"
        exit 1
    fi

    echo -e "${CYAN}--- REMOTE MODE INSTALL Step 3: Configure remote-mode ---${NC}"
    # Create remote-mode marker file
    touch "${KITTYHACK_INSTALL_DIR}/.remote-mode"

    # Camera source on remote nodes. A leftover config.ini is imported on first start.
    if [ -f "${KITTYHACK_INSTALL_DIR}/config.ini" ]; then
        sed -i "s/^camera_source\s*=\s*.*/camera_source = ip_camera/" "${KITTYHACK_INSTALL_DIR}/config.ini" || true
    fi
    upsert_db_setting "${KITTYHACK_INSTALL_DIR}/kittyhack.db" "CAMERA_SOURCE" "\"ip_camera\"" || true
    set_config_language "${KITTYHACK_INSTALL_DIR}"

    echo -e "${CYAN}--- REMOTE MODE INSTALL Step 4: Install/enable kittyhack.service ---${NC}"
    cat << EOF > /etc/systemd/system/kittyhack.service
[Unit]
Description=KittyHack WebGUI
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=${KITTYHACK_INSTALL_DIR}
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${KITTYHACK_INSTALL_DIR}/.venv/bin"
ExecStartPre=/bin/bash ${KITTYHACK_INSTALL_DIR}/setup/ensure_venv.sh --apply --root ${KITTYHACK_INSTALL_DIR}
ExecStart=${KITTYHACK_INSTALL_DIR}/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 80 --ws-ping-interval 30 --ws-ping-timeout 300
Restart=always
RestartSec=5
TimeoutStartSec=3600

KillSignal=SIGTERM
KillMode=mixed
TimeoutStopSec=30
SuccessExitStatus=SIGKILL

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable kittyhack.service
    if ! systemctl restart kittyhack.service; then
        echo -e "${RED}Failed to start kittyhack.service.${NC}"
        exit 1
    fi

    if ! systemctl is-active --quiet kittyhack.service; then
        echo -e "${RED}kittyhack.service is not active after restart.${NC}"
        exit 1
    fi

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
        GIT_TAG=$(fetch_latest_stable_release_tag || true)
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

    # Set configured UI language based on the setup language.
    set_config_language "${KITTYHACK_INSTALL_DIR}"

    # First-time install only. camera_dependencies.txt exists only after checkout,
    # and the driver debs must be in place before kittyhack_control starts the camera.
    if [[ "${INSTALL_CAMERA_DRIVERS:-false}" == true ]]; then
        reinstall_camera_drivers || ((FAIL_COUNT++))
    fi

    echo -e "${CYAN}--- KITTYHACK INSTALL Step 2: Set up Python virtual environment ---${NC}"
    if ! bash "${KITTYHACK_INSTALL_DIR}/setup/ensure_venv.sh" --bootstrap --root "${KITTYHACK_INSTALL_DIR}"; then
        ((FAIL_COUNT++))
        echo -e "${RED}Failed to create Python virtualenv / install dependencies.${NC}"
        return 1
    fi

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
ExecStartPre=/bin/bash ${KITTYHACK_INSTALL_DIR}/setup/ensure_venv.sh --apply --root ${KITTYHACK_INSTALL_DIR}
ExecStart=${KITTYHACK_INSTALL_DIR}/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 80 --ws-ping-interval 30 --ws-ping-timeout 300
Restart=always
RestartSec=5
TimeoutStartSec=3600

KillSignal=SIGTERM
KillMode=mixed
TimeoutStopSec=30
SuccessExitStatus=SIGKILL

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
ExecStartPre=/bin/bash ${KITTYHACK_INSTALL_DIR}/setup/ensure_venv.sh --apply --root ${KITTYHACK_INSTALL_DIR}
ExecStart=${KITTYHACK_INSTALL_DIR}/.venv/bin/python -m src.kittyhack_control
Restart=always
RestartSec=2
TimeoutStartSec=3600

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

# Same sequence as the System-tab "Reinstall camera driver" action:
# drop libcamera SONAMEs newer than 0.3, then dpkg -i the pinned debs.
# dpkg does not consult the Kittyflap apt pin, so this works on a first install
# that already has libcamera-ipa 0.2.0 held, and on one that has no libcamera yet.
remove_libcamera_packages_above_0_3() {
    echo -e "${GREY}Uninstalling libcamera packages above version 0.3...${NC}"
    local status pkg name major minor
    local found=false
    while read -r status pkg; do
        [[ "$status" == ii* ]] || continue
        name="${pkg%%:*}"
        if [[ "$name" =~ ^libcamera([0-9]+)\.([0-9]+)$ ]]; then
            major="${BASH_REMATCH[1]}"
            minor="${BASH_REMATCH[2]}"
            if (( major > 0 || minor > 3 )); then
                found=true
                echo -e "${GREY}Uninstalling ${name}...${NC}"
                if ! apt-get remove -y "$name"; then
                    echo -e "${YELLOW}Failed to remove ${name}. Continuing with the pinned driver install.${NC}"
                fi
            fi
        fi
    done < <(dpkg-query -W -f '${db:Status-Abbrev} ${Package}\n' 2>/dev/null || true)
    if [[ "$found" == false ]]; then
        echo -e "${GREY}No libcamera packages above version 0.3 installed.${NC}"
    fi
}

reinstall_camera_drivers() {
    echo -e "${CYAN}--- CAMERA DRIVER REINSTALL ---${NC}"
    local base_url="https://github.com/floppyFK/kittyhack-dependencies/raw/refs/heads/main/camera"
    local dependencies_file="${KITTYHACK_INSTALL_DIR}/camera_dependencies.txt"
    local download_dir="/tmp/camera_drivers"
    local package
    local -a deb_paths=()

    if [[ ! -f "$dependencies_file" ]]; then
        echo -e "${RED}Dependencies file $dependencies_file not found. Please report this in the GitHub repository.${NC}"
        return 1
    fi

    mkdir -p "$download_dir"
    rm -f "${download_dir}"/*.deb

    while IFS= read -r package || [[ -n "$package" ]]; do
        package="${package%%$'\r'}"
        package="${package#"${package%%[![:space:]]*}"}"
        package="${package%"${package##*[![:space:]]}"}"
        [[ -z "$package" || "$package" == \#* ]] && continue
        echo -e "${GREY}Downloading $package...${NC}"
        if ! curl -fsSL -o "${download_dir}/${package}" "${base_url}/${package}"; then
            echo -e "${RED}Failed to download $package.${NC}"
            rm -rf "$download_dir"
            return 1
        fi
        deb_paths+=("${download_dir}/${package}")
    done < "$dependencies_file"

    if [[ ${#deb_paths[@]} -eq 0 ]]; then
        echo -e "${RED}No camera packages listed in ${dependencies_file}.${NC}"
        rm -rf "$download_dir"
        return 1
    fi

    remove_libcamera_packages_above_0_3

    echo -e "${GREY}Installing downloaded packages...${NC}"
    if ! dpkg -i "${deb_paths[@]}"; then
        echo -e "${RED}Failed to install some packages.${NC}"
        rm -rf "$download_dir"
        return 1
    fi

    echo -e "${GREEN}All packages installed successfully.${NC}"
    rm -rf "$download_dir"
    return 0
}

# Main script logic

# Defaults / CLI flags
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
if [[ -f "${SCRIPT_DIR}/../requirements.txt" && -f "${SCRIPT_DIR}/../requirements_remote.txt" ]]; then
    KITTYHACK_ROOT=$(realpath "${SCRIPT_DIR}/..")
else
    KITTYHACK_ROOT=""
fi

# When true: do not clone from GitHub, use this repo folder as install source.
USE_LOCAL_SOURCES=false

# The directory where kittyhack will run from (venv, kittyhack.db).
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
    if [[ -z "$KITTYHACK_ROOT" || "$KITTYHACK_ROOT" == "/" ]]; then
        echo -e "${RED}Local sources requested, but no valid source directory detected. Use --sources-dir=/path/to/kittyhack.${NC}"
        exit 1
    fi
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
        echo -e "${RED}Failed to install uv. Please install it manually (https://astral.sh/uv) or install python3.14 via apt.${NC}"
        return 1
    fi
}

ensure_python311_available() {
    # Back-compat wrapper: preferred path is setup/ensure_venv.sh reading REQUIRED_PYTHON.
    local required="3.14"
    if [[ -f "$(dirname "${BASH_SOURCE[0]}")/REQUIRED_PYTHON" ]]; then
        required="$(tr -d '[:space:]' < "$(dirname "${BASH_SOURCE[0]}")/REQUIRED_PYTHON")"
    elif [[ -f "${KITTYHACK_INSTALL_DIR:-}/setup/REQUIRED_PYTHON" ]]; then
        required="$(tr -d '[:space:]' < "${KITTYHACK_INSTALL_DIR}/setup/REQUIRED_PYTHON")"
    fi
    if command -v "python${required}" >/dev/null 2>&1; then
        return 0
    fi

    echo -e "${YELLOW}python${required} not found. Trying to install python${required} via apt...${NC}"
    apt-get update -y >/dev/null 2>&1 || true
    if apt-get install -y "python${required}" "python${required}-venv" "python${required}-dev" >/dev/null 2>&1; then
        if command -v "python${required}" >/dev/null 2>&1; then
            return 0
        fi
    fi

    echo -e "${YELLOW}python${required} not available via apt. Falling back to uv-managed Python ${required}...${NC}"
    ensure_uv_installed || return 1
    uv python install "$required" || return 1
    return 0
}

create_venv_py311() {
    # Deprecated name kept for compatibility; delegates to ensure_venv.sh --bootstrap --skip-pip
    # so callers that still pip-install themselves keep working.
    local venv_path="$1"
    local root
    root="$(cd "$(dirname "$venv_path")/.." 2>/dev/null && pwd)" || root="$(dirname "$venv_path")"
    # When called as create_venv_py311 .venv from install dir, parent is install root.
    if [[ "$(basename "$venv_path")" == ".venv" ]]; then
        root="$(cd "$(dirname "$venv_path")" && pwd)"
    fi
    local script="${root}/setup/ensure_venv.sh"
    if [[ -f "$script" ]]; then
        bash "$script" --bootstrap --root "$root" --skip-pip
        return $?
    fi

    local required="3.14"
    if [[ -f "${root}/setup/REQUIRED_PYTHON" ]]; then
        required="$(tr -d '[:space:]' < "${root}/setup/REQUIRED_PYTHON")"
    fi
    if [[ -d "$venv_path" ]]; then
        rm -rf "$venv_path"
    fi
    ensure_python311_available || return 1
    if command -v "python${required}" >/dev/null 2>&1; then
        "python${required}" -m venv "$venv_path"
        return $?
    fi
    uv venv --python "$required" "$venv_path"
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
            install_full || ((FAIL_COUNT++))
            break
            ;;
        2)
            echo -e "${CYAN}Reinstalling camera drivers...${NC}"
            reinstall_camera_drivers || ((FAIL_COUNT++))
            break
            ;;
        3)
            echo -e "${CYAN}Updating to the latest version of Kittyhack...${NC}"
            INSTALL_LEGACY_KITTYHACK=0
            install_update || ((FAIL_COUNT++))
            break
            ;;
        4)
            echo -e "${CYAN}Installing Kittyhack in remote-mode...${NC}"
            INSTALL_LEGACY_KITTYHACK=0
            install_remote_mode || ((FAIL_COUNT++))
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
