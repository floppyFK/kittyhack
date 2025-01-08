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

# Ensure the script is running as root
if [ "$EUID" -ne 0 ]; then
  echo "This script must be run as root. Switching to root now..."
  exec sudo bash "$0" "$@"
fi

# Get the current IP address
CURRENT_IP=$(hostname -I | awk '{print $1}')

FAIL_COUNT=0


# Function to check if a service is active
is_service_active() {
    systemctl is-active --quiet "$1"
}

# Function to check if a line in crontab is already commented
is_cron_line_commented() {
    local pattern=$1
    sudo crontab -l 2>/dev/null | grep -E "^#.*${pattern}" > /dev/null
}

# Function to disable and mask services
disable_and_mask_service() {
    local SERVICE="$1"
    if is_service_active "$SERVICE"; then
        echo -e "${GREY}Stopping and disabling ${SERVICE} service...${NC}"
        systemctl stop "$SERVICE"
        systemctl disable "$SERVICE"
        systemctl mask "$SERVICE"
        systemctl daemon-reload
        
        if is_service_active "$SERVICE"; then
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to disable and mask ${SERVICE} service.${NC}"
            echo -e "${RED}WARNING: This would lead to an interference with the KittyHack service!${NC}"
        fi
        echo -e "${GREEN}${SERVICE} service disabled and masked.${NC}"
    else
        echo -e "${GREY}${SERVICE} service is already disabled or inactive.${NC}"
    fi
}

# Full installation process
install_full() {
    echo -e "${CYAN}--- BASE INSTALL Step 1: Disable unwanted services ---${NC}"
    disable_and_mask_service "remote-iot"
    disable_and_mask_service "manager"
    disable_and_mask_service "kwork"

    echo -e "${CYAN}--- BASE INSTALL Step 2: Check and resize swapfile if necessary ---${NC}"
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

    echo -e "${CYAN}--- BASE INSTALL Step 3: Comment out unwanted crontab entries ---${NC}"
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

    echo -e "${CYAN}--- BASE INSTALL Step 4: Rename remote-iot paths ---${NC}"
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

    echo -e "${CYAN}--- BASE INSTALL Step 5: Clean up old manager logs ---${NC}"
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
        gstreamer1.0-plugins-bad
        gstreamer1.0-plugins-ugly
        gstreamer1.0-libcamera
    )
    for pkg in "${GSTREAMER_PACKAGES[@]}"; do
        if dpkg -l | grep -q "$pkg"; then
            echo -e "${GREY}$pkg is already installed. Skipping.${NC}"
        else
            echo -e "${GREY}Installing $pkg...${NC}"
            apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 install -y "$pkg"
            if dpkg -l | grep -q "$pkg"; then
                echo -e "${GREEN}$pkg installed successfully.${NC}"
            else
                ((FAIL_COUNT++))
                echo -e "${RED}Failed to install $pkg.${NC}"
            fi
        fi
    done


    echo -e "${CYAN}--- BASE INSTALL Step 7: Install python ---${NC}"
    if ! python3.11 --version &>/dev/null; then
        echo -e "${GREY}Python 3.11 is not installed. Installing...${NC}"
        apt-get -o Acquire::http::Timeout=120 -o Acquire::Retries=5 install -y python3.11 python3.11-venv python3.11-dev
        if python3.11 --version &>/dev/null; then
            echo -e "${GREEN}Python 3.11 installed successfully.${NC}"
        else
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to install Python 3.11.${NC}"
        fi
    else
        echo -e "${GREEN}Python 3.11 is already installed.${NC}"
    fi

    install_kittyhack
}

# Install or update KittyHack
install_kittyhack() {
    echo -e "${CYAN}--- KITTYHACK UPDATE Step 1: Set up KittyHack ---${NC}"
    if systemctl is-active --quiet kittyhack.service; then
        echo -e "${GREY}Stopping existing KittyHack service...${NC}"
        systemctl stop kittyhack.service
    fi
    
    if [[ -d /root/kittyhack ]]; then
        echo -e "${GREY}Existing KittyHack repository found. Checking for updates...${NC}"

        echo -e "${GREY}Cleaning the repository...${NC}"
        git -C /root/kittyhack restore .
        git -C /root/kittyhack clean -fd
        
        echo -e "${GREY}Fetching latest tags...${NC}"
        git -C /root/kittyhack fetch --tags --quiet
        if [[ $? -eq 0 ]]; then
            LATEST_TAG=$(git -C /root/kittyhack describe --tags `git -C /root/kittyhack rev-list --tags --max-count=1`)
            echo -e "${GREY}Checking out latest tag: ${LATEST_TAG}${NC}"
            git -C /root/kittyhack checkout ${LATEST_TAG} --quiet
            if [[ $? -eq 0 ]]; then
                echo -e "${GREEN}Repository updated to latest tag: ${LATEST_TAG}${NC}"
            else
                ((FAIL_COUNT++))
                echo -e "${RED}Failed to checkout the latest tag. Please check your internet connection.${NC}"
            fi
        else
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to fetch tags. Please check your internet connection.${NC}"
        fi
    else
        echo -e "${GREY}Cloning KittyHack repository...${NC}"
        git clone https://github.com/floppyFK/kittyhack.git /root/kittyhack --quiet
        echo -e "${GREY}Fetching latest tags...${NC}"
        git -C /root/kittyhack fetch --tags --quiet
        if [[ $? -eq 0 ]]; then
            LATEST_TAG=$(git -C /root/kittyhack describe --tags `git -C /root/kittyhack rev-list --tags --max-count=1`)
            echo -e "${GREY}Checking out latest tag: ${LATEST_TAG}${NC}"
            git -C /root/kittyhack checkout ${LATEST_TAG} --quiet
            if [[ $? -eq 0 ]]; then
                echo -e "${GREEN}Repository updated to latest tag: ${LATEST_TAG}${NC}"
            else
                ((FAIL_COUNT++))
                echo -e "${RED}Failed to checkout the latest tag. Please check your internet connection.${NC}"
            fi
        else
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to fetch tags. Please check your internet connection.${NC}"
        fi
        if [[ $? -eq 0 ]]; then
            echo -e "${GREEN}Repository cloned successfully.${NC}"
        else
            ((FAIL_COUNT++))
            echo -e "${RED}Failed to clone the repository. Please check your internet connection.${NC}"
        fi
    fi

    echo -e "${CYAN}--- KITTYHACK UPDATE Step 2: Set up Python virtual environment ---${NC}"
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

    echo -e "${CYAN}--- KITTYHACK UPDATE Step 3: Install and start KittyHack service ---${NC}"
    cp /root/kittyhack/setup/kittyhack.service /etc/systemd/system/kittyhack.service
    systemctl daemon-reload
    systemctl enable kittyhack.service
    systemctl start kittyhack.service
    if systemctl is-active --quiet kittyhack.service; then
        echo -e "${GREEN}KittyHack service installed and started successfully.${NC}"
    else
        echo -e "${RED}Failed to start KittyHack service.${NC}"
    fi
}

# Main script logic


# Check for command-line arguments or prompt for a choice 
MODE=$1
ERRMSG=""

while true; do
    if [[ -z "$MODE" ]]; then

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
        echo -e "${CYAN}Welcome to the KittyHack Setup Script!${NC}"
        # echo -e "This script provides the following options:\n"
        # echo -e "${BLUE}${FMTBOLD}install${FMTDEF}${NC}: Run the full setup (disable unwanted services 'remote-iot'"
        # echo -e "         and 'manager', install KittyHack)."
        # echo -e "${BLUE}${FMTBOLD}update${FMTDEF}${NC}:  Runs only the update (or initial installation, if not yet done) of"
        # echo -e "         the KittyHack application. No system configuration will be changed.\n"

        # NOTE textbox
        echo -e "+---------------------------------- NOTE ----------------------------------+"
        echo -e "| The ${BLUE}${FMTBOLD}install${FMTDEF}${NC} option will disable and remove these services:               |"
        echo -e "| - ${FMTBOLD}remote-iot${FMTDEF}: A potentially risky service that could allow unauthorized  |"
        echo -e "|   access to your device from the internet. With the manufacturer of the  |"
        echo -e "|   KittyFlap now bankrupt, this service is obsolete and unnecessary. See  |"
        echo -e "|   ${CYAN}https://remoteiot.com${NC} for details.                                     |"
        echo -e "| - ${FMTBOLD}manager${FMTDEF}: Previously used for update checks of the original KittyFlap   |"
        echo -e "|   software. As the manufacturer is no longer operational, this service   |"
        echo -e "|   is no longer required.                                                 |"
        echo -e "| - ${FMTBOLD}kwork${FMTDEF}: The original KittyFlap software. This service is no longer      |"
        echo -e "|   required and must be disabled for KittyHack to work properly.          |"
        echo -e "+--------------------------------------------------------------------------+"

        # Version dependency note
        echo -e "+------------------------------- NOTE#2 -----------------------------------+"
        echo -e "| Even if you have KittyHack v1.1.0 or lower installed already once in the |"
        echo -e "| past, you must run the full installation once again, to install the      |"
        echo -e "| required dependencies. After that, future updates can be installed       |"
        echo -e "| directly via the web interface of Kittyhack.                             |"
        echo -e "+--------------------------------------------------------------------------+"
        

        echo -e "${ERRMSG}" 
        echo -e "${CYAN}Please enter your choice:${NC} (${BLUE}${FMTBOLD}i${FMTDEF}${NC})nstall | (${BLUE}${FMTBOLD}q${FMTDEF}${NC})uit"
        read -r MODE
    fi

    # Handle the input
    case "$MODE" in
        install|i)
            echo -e "${CYAN}Running full installation...${NC}"
            install_full
            break
            ;;
        # update|u)
        #     echo -e "Skipping installation steps..."
        #     echo -e "${CYAN}Running update process...${NC}"
        #     install_kittyhack
        #     break
        #     ;;
        q|"")
            echo -e "${YELLOW}Quitting installation.${NC}"
            exit 0
            ;;
        *)
            ERRMSG="${RED}Invalid choice. Please enter 'i' or 'q'.${NC}\n"
            MODE=""
            ;;
    esac
done

if systemctl is-active --quiet kittyhack.service; then
    if [ $FAIL_COUNT -eq 0 ]; then
        echo -e "\n${GREEN}Setup complete!${NC}\n"
        echo -e "Open ${CYAN}http://${CURRENT_IP}${NC} in your browser to start with KittyHack!\n"
    else
        echo -e "\n${YELLOW}Setup complete with warnings. Please check the logs for more details.${NC}\n"
        echo -e "You could still try to open ${CYAN}http://${CURRENT_IP}${NC} in your browser to start with KittyHack.\n"
    fi
else
    echo -e "\n${RED}Setup failed. Please check the logs for more details.${NC}\n"
fi
