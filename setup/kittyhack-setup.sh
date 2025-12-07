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
        python3.11
        python3.11-venv
        python3.11-dev
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

    if ! python3.11 -m pip --version &>/dev/null; then
        echo -e "${GREY}Python pip is not installed. Installing...${NC}"
        # Wait for any existing package operations to complete
        while fuser /var/lib/dpkg/lock >/dev/null 2>&1 || fuser /var/lib/apt/lists/lock >/dev/null 2>&1 || fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
            echo -e "${GREY}Waiting for other package operations to complete...${NC}"
            sleep 5
        done

        # Try to install pip with retries
        for i in {1..5}; do
            if apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 install -y python3-pip; then
                break
            else
                echo -e "${YELLOW}Attempt $i of 5 failed. Retrying in 5 seconds...${NC}"
                sleep 5
            fi
        done

        if python3.11 -m pip --version &>/dev/null; then
            echo -e "${GREEN}Python 3.11 pip installed successfully.${NC}"
        else
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to install Python 3.11 pip.${NC}"
        fi
    else
        echo -e "${GREEN}Python 3.11 pip is already installed.${NC}"
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

    install_kittyhack
}

# Update Kittyhack only
install_update() {
    echo -e "${CYAN}--- Check internet connection ---${NC}"
    if ! ping -c 1 google.com &>/dev/null; then
        echo -e "${RED}No internet connection detected. Please check your connection and try again.${NC}"
        exit 1
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
    echo -e "${CYAN}--- KITTYHACK INSTALL Step 1: Clone KittyHack repository ---${NC}"
    if [[ -d /root/kittyhack ]]; then
        echo -e "${GREY}Existing KittyHack repository found. Backing up database and config.ini...${NC}"
        
        # Backup important files if they exist
        [ -f /root/kittyhack/config.ini ] && cp /root/kittyhack/config.ini /tmp/config.ini.bak
        [ -f /root/kittyhack/kittyhack.db ] && cp /root/kittyhack/kittyhack.db /tmp/kittyhack.db.bak
        
        # Remove old repository
        echo -e "${GREY}removing kittyhack installation...${NC}"
        rm -rf /root/kittyhack
    fi

    echo -e "${GREY}Cloning KittyHack repository...${NC}"
    git clone https://github.com/floppyFK/kittyhack.git /root/kittyhack --quiet
    if [[ $? -eq 0 ]]; then
        echo -e "${GREEN}Repository cloned successfully.${NC}"
    else
        ((FAIL_COUNT++))
        echo -e "${RED}Failed to clone the repository. Please check your internet connection.${NC}"
    fi

    if [ $INSTALL_LEGACY_KITTYHACK -eq 0 ]; then
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

    if [[ -n "$GIT_TAG" ]]; then
        echo -e "${GREY}Checking out ${GIT_TAG}...${NC}"
        git -C /root/kittyhack fetch --tags --quiet
        git -C /root/kittyhack checkout ${GIT_TAG} --quiet
        if [[ $? -eq 0 ]]; then
            echo -e "${GREEN}Repository updated to ${GIT_TAG}${NC}"
        else
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to checkout ${GIT_TAG}. Please check your internet connection.${NC}"
        fi
    else
        ((FAIL_COUNT++))
        echo -e "${RED}Failed to fetch tags. Please check your internet connection.${NC}"
    fi
    
    # Restore backed up files if they exist
    if [ -f /tmp/config.ini.bak ]; then
        cp /tmp/config.ini.bak /root/kittyhack/config.ini && rm -f /tmp/config.ini.bak
    fi
    if [ -f /tmp/kittyhack.db.bak ]; then
        cp /tmp/kittyhack.db.bak /root/kittyhack/kittyhack.db && rm -f /tmp/kittyhack.db.bak
    fi

    echo -e "${CYAN}--- KITTYHACK INSTALL Step 2: Set up Python virtual environment ---${NC}"
    python3.11 -m venv /root/kittyhack/.venv
    source /root/kittyhack/.venv/bin/activate
    pip install --timeout 120 --retries 10 -r /root/kittyhack/requirements.txt
    if ! pip install --timeout 120 --retries 10 -r /root/kittyhack/requirements.txt; then
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


    echo -e "${CYAN}--- KITTYHACK INSTALL Step 4: Install and start KittyHack service ---${NC}"
    cp /root/kittyhack/setup/kittyhack.service /etc/systemd/system/kittyhack.service
    systemctl daemon-reload
    systemctl enable kittyhack.service
    systemctl start kittyhack.service
    if systemctl is-active --quiet kittyhack.service; then
        echo -e "${GREEN}KittyHack service installed and started successfully.${NC}"
    else
        echo -e "${RED}Failed to start KittyHack service.${NC}"
    fi

    # Log system journal information
    for i in {1..5}; do
        echo -n "."
        sleep 1
    done
    echo # move to the next line after the loop
    journalctl -n 20000 > "${LOGPATH}/journal.log"
    journalctl -n 1000 -u kwork > "${LOGPATH}/journal_kwork.log"
    journalctl -n 1000 -u kittyhack > "${LOGPATH}/journal_kittyhack.log"
    cp /root/kittyhack/kittyhack.log "${LOGPATH}/kittyhack.log"
    dpkg-query -W > "${LOGPATH}/installed_packages_after_kittyhack_setup.log"
    systemctl list-units --type=service --all > "${LOGPATH}/systemd-services_after_kittyhack_setup.log"
}

reinstall_camera_drivers() {
    echo -e "${CYAN}--- CAMERA DRIVER REINSTALL ---${NC}"
    # Download and install camera drivers
    local base_url="https://github.com/floppyFK/kittyhack-dependencies/raw/refs/heads/main/camera"
    local dependencies_file="/root/kittyhack/camera_dependencies.txt"
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

# Default language
LANGUAGE="en"

# Check for language argument or environment variable
if [ -n "$1" ]; then
  LANGUAGE="$1"
elif [ -n "$SCRIPT_LANG" ]; then
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
        echo -e "(${BLUE}${FMTBOLD}1${FMTDEF}${NC}) Erstmalige Installation von Kittyhack ausführen"
        echo -e "(${BLUE}${FMTBOLD}2${FMTDEF}${NC}) Kameratreiber erneut installieren (bitte nur ausführen, wenn du keine Live-Bilder siehst. Ist inzwischen auch über die Kittyhack Web-Oberfläche möglich)"
        echo -e "(${BLUE}${FMTBOLD}3${FMTDEF}${NC}) Update auf die neueste Version von Kittyhack (bitte nur ausführen, wenn du bereits Kittyhack installiert hast)"
        echo -e "(${BLUE}${FMTBOLD}b${FMTDEF}${NC})eenden"
    else
        echo -e "${CYAN}Please choose the desired option:${NC}"
        echo -e "(${BLUE}${FMTBOLD}1${FMTDEF}${NC}) Run initial installation of Kittyhack"
        echo -e "(${BLUE}${FMTBOLD}2${FMTDEF}${NC}) Reinstall camera drivers (only run if you don't see live images. Can also be done via the Kittyhack web interface now)"
        echo -e "(${BLUE}${FMTBOLD}3${FMTDEF}${NC}) Update to the latest version of Kittyhack (only run if you already have Kittyhack installed)"
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
        q|b)
            echo -e "${YELLOW}Quitting installation.${NC}"
            exit 0
            ;;
        *)
            if [ "$LANGUAGE" == "de" ]; then
                ERRMSG="${RED}Ungültige Eingabe. Bitte '1', '2', '3' oder 'b' eingeben.${NC}\n"
            else
                ERRMSG="${RED}Invalid choice. Please enter '1', '2', '3' or 'q'.${NC}\n"
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
            if [ $INSTALL_LEGACY_KITTYHACK -eq 0 ]; then
                echo -e "${GREY}Falls auf deiner Kittyflap bereits viele Bilder gespeichert sind, kann es einige Zeit dauern, bis du die KittyHack Seite in deinem Browser aufrufen kannst. Bitte habe etwas Geduld.${NC}"
                echo -e "${GREY}Wenn alle Bilder importiert wurden, starte bitte anschließend deine Kittyflap über die Weboberfläche in der Sektion 'System' einmal neu, um alle Änderungen zu übernehmen.${NC}"
            fi
        else
            echo -e "\n${GREEN}Setup complete!${NC}\n"
            echo -e "Open ${CYAN}http://${CURRENT_IP}${NC} in your browser to access your Kittyflap!\n"
            if [ $INSTALL_LEGACY_KITTYHACK -eq 0 ]; then
                echo -e "${GREY}If your Kittyflap already has many images stored, it may take some time before you can access the KittyHack page in your browser. Please be patient.${NC}"
                echo -e "${GREY}Once all images have been imported, please restart your Kittyflap via the web interface in the 'System' section to apply all changes.${NC}"
            fi
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
