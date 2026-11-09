#!/bin/bash
# Install the Kittyhack model server on a Mac with Apple silicon.
#
# Run it as your normal user, not with sudo. The server lives in
# ~/Library/Application Support/kittyhack-model-server with its own virtualenv
# and starts as a LaunchAgent whenever you log in. Training runs on the Apple
# GPU (mps).
#
#   install_model_server_macos.sh install   [--lang en|de] [--port N] [--sources DIR] [--noninteractive]
#   install_model_server_macos.sh uninstall [--lang en|de] [--yes]
#
# Running install again updates the code and keeps config.ini.

set -euo pipefail

LANG_UI="en"
SOURCES=""
NONINTERACTIVE=0
ASSUME_YES=0
PORT=""
NO_AUTOSTART="${MODEL_SERVER_NO_AUTOSTART:-0}"

LABEL="de.kittyhack.model-server"
INSTALL_DIR="${MODEL_SERVER_INSTALL_DIR:-$HOME/Library/Application Support/kittyhack-model-server}"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
SOURCE_ZIP_URL="https://github.com/floppyFK/kittyhack/archive/refs/heads/main.zip"

usage() {
    echo "Usage: $0 install|uninstall [--lang en|de] [--port N] [--sources DIR] [--noninteractive] [--yes]" >&2
    exit 2
}

if [[ $# -lt 1 ]]; then
    usage
fi
COMMAND="$1"
shift

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sources) SOURCES="${2:-}"; shift 2 ;;
        --sources=*) SOURCES="${1#*=}"; shift ;;
        --lang) LANG_UI="${2:-en}"; shift 2 ;;
        --lang=*) LANG_UI="${1#*=}"; shift ;;
        --port) PORT="${2:-}"; shift 2 ;;
        --port=*) PORT="${1#*=}"; shift ;;
        --noninteractive) NONINTERACTIVE=1; shift ;;
        --yes) ASSUME_YES=1; shift ;;
        *) echo "Unknown argument: $1" >&2; usage ;;
    esac
done

if [[ "$LANG_UI" != "de" ]]; then
    LANG_UI="en"
fi

say() {
    if [[ "$LANG_UI" == "de" ]]; then
        printf '%s\n' "$2" >&2
    else
        printf '%s\n' "$1" >&2
    fi
}

answer_is_yes() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        y|yes|j|ja) return 0 ;;
        *) return 1 ;;
    esac
}

valid_port() {
    [[ "$1" =~ ^[0-9]+$ ]] && (( "$1" >= 1 && "$1" <= 65535 ))
}

check_platform() {
    if [[ "$(uname -s)" != "Darwin" ]]; then
        say "This script is for macOS. On Linux use setup/install_model_server.sh." \
            "Dieses Skript ist für macOS. Unter Linux setup/install_model_server.sh verwenden."
        exit 1
    fi
    if [[ "$(id -u)" -eq 0 ]]; then
        say "Run this script as your normal user, without sudo." \
            "Dieses Skript als normaler Benutzer ausführen, ohne sudo."
        exit 1
    fi
    # PyTorch no longer ships wheels for Intel Macs, and only Apple silicon has mps.
    if [[ "$(uname -m)" != "arm64" && "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != "1" ]]; then
        say "A Mac with Apple silicon (M1 or newer) is required." \
            "Ein Mac mit Apple-Chip (M1 oder neuer) wird benötigt."
        exit 1
    fi
}

find_python() {
    # PyTorch 2.9 has macOS wheels for Python 3.10 to 3.13.
    local candidate version
    for candidate in \
        python3.12 python3.13 python3.11 python3.10 \
        /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.13 \
        /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 \
        python3
    do
        if ! command -v "$candidate" >/dev/null 2>&1; then
            continue
        fi
        version="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
        case "$version" in
            3.10|3.11|3.12|3.13)
                command -v "$candidate"
                return 0
                ;;
        esac
    done
    return 1
}

source_root() {
    if [[ -n "$SOURCES" ]]; then
        if [[ ! -f "${SOURCES}/model-server/server.py" ]]; then
            say "No model-server/server.py under ${SOURCES}" "Keine model-server/server.py unter ${SOURCES}"
            exit 1
        fi
        (cd "$SOURCES" && pwd)
        return
    fi
    local here
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [[ -f "${here}/../model-server/server.py" ]]; then
        (cd "${here}/.." && pwd)
        return
    fi
    local tmp
    tmp="$(mktemp -d -t kittyhack-src)"
    say "Downloading Kittyhack sources…" "Lade die Kittyhack-Quellen herunter…"
    curl -fsSL "$SOURCE_ZIP_URL" -o "${tmp}/kittyhack.zip"
    unzip -q "${tmp}/kittyhack.zip" -d "$tmp"
    local extracted
    extracted="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d -name 'kittyhack-*' | head -n1)"
    if [[ -z "$extracted" || ! -f "${extracted}/model-server/server.py" ]]; then
        say "The downloaded sources do not contain model-server/server.py" \
            "Die heruntergeladenen Quellen enthalten keine model-server/server.py"
        exit 1
    fi
    printf '%s\n' "$extracted"
}

copy_sources() {
    local src="$1/model-server"
    local name
    mkdir -p "$INSTALL_DIR"
    for name in \
        server.py train.py config.py \
        requirements.txt requirements-torch-macos.txt config.ini.example
    do
        if [[ ! -f "${src}/${name}" ]]; then
            say "Missing ${src}/${name}" "Datei fehlt: ${src}/${name}"
            exit 1
        fi
        cp "${src}/${name}" "${INSTALL_DIR}/${name}"
    done
}

read_port() {
    local value
    value="$(awk -F= '/^[[:space:]]*port[[:space:]]*=/ { gsub(/[[:space:]]/, "", $2); print $2; exit }' \
        "${INSTALL_DIR}/config.ini" 2>/dev/null || true)"
    echo "${value:-8000}"
}

set_config_value() {
    local key="$1" value="$2"
    sed -i '' -E "s/^(${key}[[:space:]]*=[[:space:]]*).*/\\1${value}/" "${INSTALL_DIR}/config.ini"
}

prepare_config() {
    if [[ -f "${INSTALL_DIR}/config.ini" ]]; then
        return 0
    fi
    cp "${INSTALL_DIR}/config.ini.example" "${INSTALL_DIR}/config.ini"
    local chosen="$PORT"
    if [[ -z "$chosen" && "$NONINTERACTIVE" -eq 0 ]]; then
        local entered=""
        say "Port for the model server [8000]: " "Port für den Modellserver [8000]: "
        read -r entered || true
        chosen="${entered:-8000}"
    fi
    chosen="${chosen:-8000}"
    if ! valid_port "$chosen"; then
        say "Invalid port: $chosen" "Ungültiger Port: $chosen"
        exit 1
    fi
    set_config_value port "$chosen"
    # The GPU shares the main memory. A batch of 16 at image size 512 can use
    # up a 16 GB Mac until macOS ends the training.
    local mem_gb
    mem_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
    if (( mem_gb <= 16 )); then
        set_config_value batch 8
        say "This Mac has ${mem_gb} GB of memory, so training uses batch = 8." \
            "Dieser Mac hat ${mem_gb} GB Arbeitsspeicher, daher trainiert er mit batch = 8."
    fi
}

requirements_fingerprint() {
    cat "${INSTALL_DIR}/requirements.txt" "${INSTALL_DIR}/requirements-torch-macos.txt" "$1" \
        | shasum -a 256 | awk '{print $1}'
}

ensure_venv() {
    local python="$1"
    local stamp="${INSTALL_DIR}/.requirements.sha256"
    local fingerprint
    fingerprint="$(printf '%s' "$python" | requirements_fingerprint /dev/stdin)"
    local venv_python="${INSTALL_DIR}/.venv/bin/python"
    if [[ -x "$venv_python" && -f "$stamp" && "$(cat "$stamp")" == "$fingerprint" ]]; then
        say "Python environment is already up to date." "Die Python-Umgebung ist bereits aktuell."
        return 0
    fi
    if [[ ! -x "$venv_python" ]]; then
        say "Creating the model server virtualenv with $("$python" --version)…" \
            "Erstelle die virtuelle Umgebung des Modellservers mit $("$python" --version)…"
        "$python" -m venv "${INSTALL_DIR}/.venv"
    fi
    say "Installing model server dependencies. This can take several minutes." \
        "Installiere die Abhängigkeiten des Modellservers. Das kann einige Minuten dauern."
    "$venv_python" -m pip install --upgrade pip
    "$venv_python" -m pip install -r "${INSTALL_DIR}/requirements-torch-macos.txt"
    "$venv_python" -m pip install -r "${INSTALL_DIR}/requirements.txt"
    if ! "$venv_python" -c 'import torch, sys; sys.exit(0 if torch.backends.mps.is_available() else 1)'; then
        say "PyTorch does not see the Apple GPU (mps). Training will run on the CPU." \
            "PyTorch sieht die Apple-GPU (mps) nicht. Das Training läuft dann auf der CPU."
    fi
    printf '%s\n' "$fingerprint" > "$stamp"
}

write_plist() {
    mkdir -p "$(dirname "$PLIST")"
    local dir_xml log_xml
    dir_xml="$(printf '%s' "$INSTALL_DIR" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')"
    log_xml="${dir_xml}/server.log"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${dir_xml}/.venv/bin/python</string>
        <string>-u</string>
        <string>${dir_xml}/server.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${dir_xml}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>${log_xml}</string>
    <key>StandardErrorPath</key>
    <string>${log_xml}</string>
</dict>
</plist>
EOF
    plutil -lint "$PLIST" >/dev/null
}

stop_agent() {
    launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
}

start_agent() {
    stop_agent
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
}

wait_for_port() {
    local port="$1" i
    for i in $(seq 1 40); do
        if nc -z 127.0.0.1 "$port" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

allow_in_firewall() {
    # The application firewall asks about Python in a dialog, which a
    # LaunchAgent cannot show. Allow the venv's Python up front if it is on.
    local fw=/usr/libexec/ApplicationFirewall/socketfilterfw
    if [[ ! -x "$fw" ]] || ! "$fw" --getglobalstate 2>/dev/null | grep -qi "enabled"; then
        return 0
    fi
    local binary
    binary="$("${INSTALL_DIR}/.venv/bin/python" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
    if [[ "$NONINTERACTIVE" -eq 1 ]]; then
        say "The macOS firewall is on. Allow incoming connections for ${binary} so Kittyhack can reach the server." \
            "Die macOS-Firewall ist an. Eingehende Verbindungen für ${binary} erlauben, damit Kittyhack den Server erreicht."
        return 0
    fi
    local answer=""
    say "The macOS firewall is on. Allow incoming connections for the model server's Python? This asks for your password. (y/N): " \
        "Die macOS-Firewall ist an. Eingehende Verbindungen für das Python des Modellservers erlauben? Dafür wird dein Passwort abgefragt. (y/N): "
    read -r answer || true
    if ! answer_is_yes "$answer"; then
        say "Leaving the firewall unchanged. Kittyhack may not reach the server." \
            "Firewall bleibt unverändert. Kittyhack erreicht den Server dann eventuell nicht."
        return 0
    fi
    sudo "$fw" --add "$binary" >/dev/null || true
    sudo "$fw" --unblockapp "$binary" >/dev/null || true
}

primary_ipv4() {
    local iface ip
    iface="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')"
    ip="$( [[ -n "$iface" ]] && ipconfig getifaddr "$iface" 2>/dev/null || true)"
    printf '%s\n' "${ip:-127.0.0.1}"
}

install_server() {
    check_platform
    local python
    if ! python="$(find_python)"; then
        say "Python 3.10 to 3.13 is required. Install it with Homebrew: brew install python@3.12" \
            "Python 3.10 bis 3.13 wird benötigt. Mit Homebrew installieren: brew install python@3.12"
        exit 1
    fi
    local src
    src="$(source_root)"
    say "Installing the model server to ${INSTALL_DIR}" "Installiere den Modellserver nach ${INSTALL_DIR}"
    stop_agent
    copy_sources "$src"
    prepare_config
    ensure_venv "$python"
    local port host
    port="$(read_port)"
    host="$(primary_ipv4)"
    if [[ "$NO_AUTOSTART" == "1" ]]; then
        say "Skipping the LaunchAgent. Start the server with: cd \"${INSTALL_DIR}\" && .venv/bin/python server.py" \
            "LaunchAgent übersprungen. Server starten mit: cd \"${INSTALL_DIR}\" && .venv/bin/python server.py"
        return 0
    fi
    write_plist
    start_agent
    allow_in_firewall
    if ! wait_for_port "$port"; then
        say "The model server did not start. See ${INSTALL_DIR}/server.log" \
            "Der Modellserver ist nicht gestartet. Siehe ${INSTALL_DIR}/server.log"
        exit 1
    fi
    say "Model server is running at http://${host}:${port}" "Modellserver läuft unter http://${host}:${port}"
    say "It starts again when you log in. Keep the Mac awake and on power while it trains." \
        "Er startet wieder, wenn du dich anmeldest. Während des Trainings den Mac wach und am Netzteil lassen."
    say "Training settings: ${INSTALL_DIR}/config.ini" "Trainingseinstellungen: ${INSTALL_DIR}/config.ini"
}

uninstall_server() {
    if [[ ! -f "$PLIST" && ! -d "$INSTALL_DIR" ]]; then
        say "No model server is installed." "Es ist kein Modellserver installiert."
        return 0
    fi
    if [[ "$ASSUME_YES" -ne 1 ]]; then
        local answer=""
        say "Remove the model server, its LaunchAgent, and ${INSTALL_DIR}? (y/N): " \
            "Modellserver, LaunchAgent und ${INSTALL_DIR} entfernen? (y/N): "
        read -r answer || true
        if ! answer_is_yes "$answer"; then
            say "Keeping the model server." "Modellserver bleibt installiert."
            return 0
        fi
    fi
    stop_agent
    rm -f "$PLIST"
    case "$INSTALL_DIR" in
        ""|"/"|"$HOME"|"$HOME/") ;;
        *) rm -rf "$INSTALL_DIR" ;;
    esac
    say "Model server removed." "Modellserver entfernt."
}

case "$COMMAND" in
    install) install_server ;;
    uninstall) uninstall_server ;;
    *) usage ;;
esac
