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
LOG_SERVER="https://kittyhack-development.fk-cloud.de"
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
        # FIXME: Just to be sure, we should mask the service even if it is not active
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

# Function to upload logs to developer server
upload_logs() {
    local archive="/tmp/kittyhack-logs-$(date +%Y%m%d-%H%M%S).tar.gz"
    
    # Create tar archive of logs
    if [ -d "${LOGPATH}" ]; then
        tar -czf "$archive" -C "$(dirname "${LOGPATH}")" "$(basename "${LOGPATH}")"
    else
        echo -e "${RED}Log directory ${LOGPATH} does not exist. Cannot create archive.${NC}"
        return 1
    fi

    # Upload logs if tar was successful
    response=$(curl -s -o /dev/null -w "%{http_code}" -X POST -F "file=@${archive}" "${LOG_SERVER}/kittyhack/upload/logs/$(basename ${archive})")
    rm -f "$archive"
}

request_log_report() {
    # Loop until a valid input (y/n) is received
    while true; do
        if [ "$LANGUAGE" == "de" ]; then
            echo -e "${CYAN}Möchtest du die Installationsprotokolle dem Entwickler von Kittyhack zur Verfügung stellen?${NC}"
            echo -e "${CYAN}Damit würdest du sehr dabei helfen, den Installer für alle Kittyflap Nutzer zu verbessern!${NC}"
            echo -e "(${BLUE}${FMTBOLD}j${FMTDEF}${NC})a | (${BLUE}${FMTBOLD}n${FMTDEF}${NC})ein"
            read -r SHARE_LOGS
            case $SHARE_LOGS in
                [JjYy]) SHARE_LOGS="y"; break ;;
                [Nn]) SHARE_LOGS="n"; break ;;
                *) echo -e "${RED}Bitte 'j' für ja oder 'n' für nein eingeben.${NC}" ;;
            esac
        else
            echo -e "${CYAN}Do you want to share the install logs with the developer of kittyhack?!${NC}"
            echo -e "${CYAN}This would be very helpful to improve the installer for all Kittyflap users!${NC}"
            echo -e "(${BLUE}${FMTBOLD}y${FMTDEF}${NC})es | (${BLUE}${FMTBOLD}n${FMTDEF}${NC})o"
            read -r SHARE_LOGS
            case $SHARE_LOGS in
                [Yy]) SHARE_LOGS="y"; break ;;
                [Nn]) SHARE_LOGS="n"; break ;;
                *) echo -e "${RED}Please enter 'y' for yes or 'n' for no.${NC}" ;;
            esac
        fi
    done
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

    # Now rename the manager executable file just to be sure
    if [ -f /root/manager ]; then
        mv /root/manager /root/manager_disabled
        echo -e "${GREEN}Manager executable renamed to manager_disabled.${NC}"
    else
        echo -e "${GREY}Manager executable not found. Skipping.${NC}"
    fi

    # Rename also the kwork executable
    if [ -f /root/kittyflap_versions/latest/main ]; then
        mv /root/kittyflap_versions/latest/main /root/kittyflap_versions/latest/main_disabled
        echo -e "${GREEN}Kwork executable renamed to main_disabled.${NC}"
    else
        echo -e "${GREY}Kwork executable not found. Skipping.${NC}"
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
    apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 update
    GSTREAMER_PACKAGES=(
        gstreamer1.0-tools
        gstreamer1.0-plugins-base
        gstreamer1.0-plugins-good
        gstreamer1.0-libcamera
    )
    echo -e "${GREY}Installing all GStreamer packages...${NC}"
    apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 install -y "${GSTREAMER_PACKAGES[@]}"
    for pkg in "${GSTREAMER_PACKAGES[@]}"; do
        if dpkg -l | grep -q "$pkg"; then
            echo -e "${GREEN}$pkg installed successfully.${NC}"
        else
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to install $pkg.${NC}"
        fi
    done


    echo -e "${CYAN}--- BASE INSTALL Step 8: Install python ---${NC}"
    PYTHON_PACKAGES=(
        python3.11
        python3.11-venv
        python3.11-dev
    )
    echo -e "${GREY}Installing all Python packages...${NC}"
    apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 install -y "${PYTHON_PACKAGES[@]}"
    for pkg in "${PYTHON_PACKAGES[@]}"; do
        if dpkg -l | grep -q "$pkg"; then
            echo -e "${GREEN}$pkg installed successfully.${NC}"
        else
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to install $pkg.${NC}"
        fi
    done

    if ! python3.11 -m pip --version &>/dev/null; then
        echo -e "${GREY}Python pip is not installed. Installing...${NC}"
        apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 install -y python3-pip
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
        apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 install -y git
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
        apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 install -y curl
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
        apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 install -y tar
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
        GIT_TAG=$(curl -s https://api.github.com/repos/floppyFK/kittyhack/releases/latest | grep -Po '"tag_name": "\K.*?(?=")')
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
        
        # Rename the kwork executable if it was renamed to main_disabled
        if [ -f /root/kittyflap_versions/latest/main_disabled ]; then
            rm /root/kittyflap_versions/latest/main
            mv /root/kittyflap_versions/latest/main_disabled /root/kittyflap_versions/latest/main
            echo -e "${GREEN}Kwork executable renamed back to main.${NC}"
        fi

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
    local files=(
        "/root/libcamera-ipa-libs/libcamera0.2_0.2.0+rpt20240418-1_arm64.deb"
        "/root/libcamera-ipa-libs/libcamera-ipa_0.2.0+rpt20240418-1_arm64.deb"
        "/root/libcamera-dependencies/rpicam-apps.deb"
        "/root/libcamera-dependencies/libcamera0.2.deb"
        "/root/libcamera-dependencies/libcamera-ipa.deb"
    )

    for file in "${files[@]}"; do
        if [[ -f "$file" ]]; then
            echo -e "${GREY}Installing $file...${NC}"
            dpkg -i "$file"
            if [[ $? -ne 0 ]]; then
                ((FAIL_COUNT++))
                echo -e "${RED}Failed to install $file.${NC}"
            else
                echo -e "${GREEN}$file installed successfully.${NC}"
            fi
        else
            echo -e "${RED}File $file not found. Please report this in the GitHub repository.${NC}"
        fi
    done
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
    echo
    if [ "$LANGUAGE" == "de" ]; then
        echo -e "Willkommen zum KittyHack-Setup!"
        echo
        echo -e "+--------------------------------- ${CYAN}WARNUNG${NC} ---------------------------------+"
        echo -e "| Es gibt Berichte über Probleme bei der Installation der neuesten Version  |"
        echo -e "| von Kittyhack. Wenn du keine Änderungen am Kittyflap-System vorgenommen   |"
        echo -e "| hast (insbesondere wenn du selbst bisher kein '${CYAN}apt upgrade${NC}' ausgeführt    |"
        echo -e "| hast), sollte die Installation der neuesten Version funktionieren.        |"
        echo -e "| Du musst dich nicht endgültig entscheiden - ein nachträglicher Wechsel    |"
        echo -e "| zwischen den Versionen ist möglich, indem du das setup einfach nochmal    |"
        echo -e "| ausführst!                                                                |"
        echo -e "|         ${CYAN}${FMTBOLD}Wenn du dir unsicher bist, installiere die Version 1.1.1${FMTDEF}${NC}          |"
        echo -e "| Was ist der Unterschied zwischen den Versionen?                           |"
        echo -e "| - Die v1.1.x basiert auf der originalen Kittyflap Software und fungiert   |"
        echo -e "|   nur als 'Client' zur Anzeige der Bilder und zur Steuerung einger        |"
        echo -e "|   weniger Funktionen.                                                     |"
        echo -e "| - Ab v1.2.0 wird die originale Kittyflap Software komplett durch eine neu |"
        echo -e "|   entwickelte Software ersetzt, die mehr Funktionen und eine bessere      |"
        echo -e "|   Kontrolle über die Katzenklappe bietet.                                 |"
        echo -e "| Zusätzlich wird der Installer fragen, ob du die Installationsprotokolle   |"
        echo -e "| mit dem Entwickler teilen möchten. ${FMTBOLD}Das Teilen der Protokolle würde mir${FMTDEF}    |"
        echo -e "| ${FMTBOLD}unglaublich helfen, den Installer zu verbessern und eventuelle${FMTDEF}            |"
        echo -e "| ${FMTBOLD}Probleme zu beheben!${FMTDEF}                                                      |"
        echo -e "+---------------------------------------------------------------------------+"
    else
        echo -e "Welcome to the KittyHack setup!"
        echo
        echo -e "+--------------------------------- ${CYAN}WARNING${NC} --------------------------------+"
        echo -e "| There have been reports of issues when installing the latest version of  |"
        echo -e "| Kittyhack. If you have not made any changes on the Kittyflap system      |"
        echo -e "| (especially if you have not run the '${CYAN}apt upgrade${NC}' command), you should be|"
        echo -e "| able to install the latest version.                                      |"
        echo -e "| Note that you do not have to make a final decision - you can switch      |"
        echo -e "| between versions by running the setup again!                             |"
        echo -e "|             ${CYAN}${FMTBOLD}If you are unsure, please install version 1.1.1${FMTDEF}${NC}              |"
        echo -e "| What is the difference between the versions?                             |"
        echo -e "| - The v1.1.x is based on the original Kittyflap software and acts as a   |"
        echo -e "|   'client' to display images and control a few functions.                |"
        echo -e "| - From v1.2.0 onwards, the original Kittyflap software is completely     |"
        echo -e "|   replaced by a newly developed software that offers more functions and  |"
        echo -e "|   better control over the cat flap.                                      |"
        echo -e "| Additionally, the installer will ask if you want to share the install    |"
        echo -e "| logs with the developer. ${FMTBOLD}Sharing the logs will help a lot to improve the${FMTDEF} |"
        echo -e "| ${FMTBOLD}installer and fix any issues!${FMTDEF}                                            |"
        echo -e "+--------------------------------------------------------------------------+"
    fi
    
    echo -e "${ERRMSG}" 
    if [ "$LANGUAGE" == "de" ]; then
        echo -e "${CYAN}Bitte die gewünschte Option auswählen:${NC}"
        echo -e "(${BLUE}${FMTBOLD}1${FMTDEF}${NC}) Kittyhack v1.1.1 installieren"
        echo -e "(${BLUE}${FMTBOLD}2${FMTDEF}${NC}) Neueste Kittyhack Version installieren (Warnung oben beachten!)"
        echo -e "(${BLUE}${FMTBOLD}3${FMTDEF}${NC}) Kameratreiber erneut installieren (bitte nur ausführen, wenn du keine Live-Bilder siehst)"
        echo -e "(${BLUE}${FMTBOLD}b${FMTDEF}${NC})eenden"
    else
        echo -e "${CYAN}Please choose the desired option:${NC}"
        echo -e "(${BLUE}${FMTBOLD}1${FMTDEF}${NC}) install v1.1.1"
        echo -e "(${BLUE}${FMTBOLD}2${FMTDEF}${NC}) install the latest version (see the warning above!)"
        echo -e "(${BLUE}${FMTBOLD}3${FMTDEF}${NC}) Reinstall camera drivers (only run if you don't see live images)"
        echo -e "(${BLUE}${FMTBOLD}q${FMTDEF}${NC})uit"
    fi
    read -r MODE

    # Handle the input
    case "$MODE" in
        1)
            echo -e "${CYAN}Installing Kittyhack v1.1.1...${NC}"
            INSTALL_LEGACY_KITTYHACK=1
            request_log_report
            install_full
            break
            ;;
        2)
            echo -e "${CYAN}Installing the latest version of Kittyhack...${NC}"
            INSTALL_LEGACY_KITTYHACK=0
            request_log_report
            install_full
            break
            ;;
        3)
            echo -e "${CYAN}Reinstalling camera drivers...${NC}"
            request_log_report
            reinstall_camera_drivers
            break
            ;;
        q|b)
            echo -e "${YELLOW}Quitting installation.${NC}"
            exit 0
            ;;
        *)
            if [ "$LANGUAGE" == "de" ]; then
                ERRMSG="${RED}Ungültige Eingabe. Bitte '1', '2' oder 'q' eingeben.${NC}\n"
            else
                ERRMSG="${RED}Invalid choice. Please enter '1', '2' or 'b'.${NC}\n"
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

if [[ "$SHARE_LOGS" == "n" ]]; then
    echo -e "${GREY}Skipping log upload.${NC}"
else
    upload_logs
fi
