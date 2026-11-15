#!/bin/bash
# Install or refresh the local Kittyhack model server.
#
# The server lives in its own directory with its own virtualenv. Next to a
# Kittyhack install that directory is a sibling (for example
# /root/kittyhack-model-server). On a machine that does not run Kittyhack it
# is /opt/kittyhack-model-server. It is not installed on a Kittyflap.
#
#   install_model_server.sh install  --sources /path/to/kittyhack [--lang en|de]
#   install_model_server.sh refresh  --sources /path/to/kittyhack [--noninteractive]
#   install_model_server.sh uninstall [--lang en|de] [--yes]

set -euo pipefail

LANG_UI="en"
SOURCES=""
NONINTERACTIVE=0
ASSUME_YES=0
PORT=""
TORCH_CHOICE=""

usage() {
    echo "Usage: $0 install|refresh|uninstall --sources DIR [--lang en|de] [--noninteractive] [--yes] [--port N] [--cpu|--cuda|--rocm]" >&2
    exit 2
}

if [[ $# -lt 1 ]]; then
    usage
fi
COMMAND="$1"
shift

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sources)
            SOURCES="${2:-}"
            shift 2
            ;;
        --sources=*)
            SOURCES="${1#*=}"
            shift
            ;;
        --lang)
            LANG_UI="${2:-en}"
            shift 2
            ;;
        --lang=*)
            LANG_UI="${1#*=}"
            shift
            ;;
        --noninteractive)
            NONINTERACTIVE=1
            shift
            ;;
        --yes)
            ASSUME_YES=1
            shift
            ;;
        --port)
            PORT="${2:-}"
            shift 2
            ;;
        --port=*)
            PORT="${1#*=}"
            shift
            ;;
        --cpu)
            TORCH_CHOICE="cpu"
            shift
            ;;
        --cuda)
            TORCH_CHOICE="cuda"
            shift
            ;;
        --rocm)
            TORCH_CHOICE="rocm"
            shift
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            ;;
    esac
done

say() {
    if [[ "$LANG_UI" == "de" ]]; then
        printf '%s\n' "$2" >&2
    else
        printf '%s\n' "$1" >&2
    fi
}

case "$(uname -s 2>/dev/null || true)" in
    MINGW*|MSYS*|CYGWIN*)
        say \
            "On Windows, open PowerShell as administrator and run setup/install_model_server.ps1." \
            "Unter Windows PowerShell als Administrator starten und setup/install_model_server.ps1 ausführen."
        exit 1
        ;;
esac

if [[ "$(id -u)" -ne 0 ]]; then
    say "Run this script as root." "Dieses Skript muss als root laufen."
    exit 1
fi

refuse_kittyflap() {
    say \
        "The model server cannot be installed on a Kittyflap. Use the remote-mode computer or another Linux PC." \
        "Der Modellserver kann nicht auf einer Kittyflap installiert werden. Nutze den Remote-Mode-Computer oder einen anderen Linux-PC."
    exit 1
}

assert_not_kittyflap() {
    local arch
    arch="$(uname -m)"
    case "$arch" in
        arm*|aarch64)
            refuse_kittyflap
            ;;
    esac
    if [[ -f /proc/device-tree/model ]] && tr -d '\0' < /proc/device-tree/model | grep -qi raspberry; then
        refuse_kittyflap
    fi
}

unit_working_directory() {
    local unit="$1"
    local wd=""
    if [[ -f "$unit" ]]; then
        wd="$(grep -E '^WorkingDirectory=' "$unit" | head -n1 | cut -d= -f2- || true)"
    fi
    printf '%s\n' "$wd"
}

detect_install_dir() {
    if [[ -n "${MODEL_SERVER_INSTALL_DIR:-}" ]]; then
        printf '%s\n' "$MODEL_SERVER_INSTALL_DIR"
        return
    fi
    local wd=""
    wd="$(unit_working_directory /etc/systemd/system/kittyhack-model-server.service)"
    if [[ -n "$wd" && "$wd" != "/" && "$wd" != "/root" && -d "$wd" ]]; then
        printf '%s\n' "$wd"
        return
    fi
    wd=""
    if command -v systemctl >/dev/null 2>&1; then
        wd="$(systemctl show -p WorkingDirectory --value kittyhack.service 2>/dev/null || true)"
    fi
    if [[ -z "$wd" || "$wd" == "/" ]]; then
        wd="$(unit_working_directory /etc/systemd/system/kittyhack.service)"
    fi
    if [[ -n "$wd" && "$wd" != "/" && -d "$wd" ]]; then
        printf '%s\n' "$(dirname "$wd")/kittyhack-model-server"
        return
    fi
    # A machine that only hosts the trainer, with no Kittyhack install.
    printf '%s\n' "/opt/kittyhack-model-server"
}

primary_ipv4() {
    local host=""
    # hostname -I is a Debian extension and fails on Arch.
    if host="$(hostname -I 2>/dev/null | awk '{print $1}')" && [[ -n "$host" ]]; then
        printf '%s\n' "$host"
        return
    fi
    host="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}' || true)"
    printf '%s\n' "${host:-127.0.0.1}"
}

read_port() {
    local file="$1"
    local value
    value="$(awk -F= '/^[[:space:]]*port[[:space:]]*=/ { gsub(/[[:space:]]/, "", $2); print $2; exit }' "$file" 2>/dev/null || true)"
    if [[ -z "$value" ]]; then
        echo 8000
    else
        echo "$value"
    fi
}

valid_port() {
    [[ "$1" =~ ^[0-9]+$ ]] && (( "$1" >= 1 && "$1" <= 65535 ))
}

copy_sources() {
    local src="$1"
    local dest="$2"
    local name
    mkdir -p "$dest"
    for name in \
        server.py train.py config.py \
        requirements.txt requirements-torch-cpu.txt requirements-torch-cuda.txt requirements-torch-rocm.txt \
        config.ini.example kittyhack-model-server.service
    do
        if [[ ! -f "${src}/model-server/${name}" ]]; then
            say "Missing ${src}/model-server/${name}" "Datei fehlt: ${src}/model-server/${name}"
            exit 1
        fi
        cp -a "${src}/model-server/${name}" "${dest}/${name}"
    done
}

requirements_fingerprint() {
    local dest="$1"
    local variant="$2"
    local torch_file="requirements-torch-${variant}.txt"
    sha256sum \
        "${dest}/requirements.txt" \
        "${dest}/${torch_file}" \
        | sha256sum \
        | awk -v variant="$variant" '{print variant " " $1}'
}

ensure_os_packages() {
    if python3 -m venv --help >/dev/null 2>&1; then
        return 0
    fi
    say "Installing Python packages…" "Installiere Python-Pakete…"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y python3 python3-venv python3-pip libgl1 libglib2.0-0 ca-certificates
}

ensure_venv() {
    local dest="$1"
    local variant="$2"
    local stamp="${dest}/.requirements.sha256"
    local fingerprint
    fingerprint="$(requirements_fingerprint "$dest" "$variant")"
    if [[ -x "${dest}/.venv/bin/python" && -f "$stamp" && "$(cat "$stamp")" == "$fingerprint" ]]; then
        say "Python environment is already up to date." "Die Python-Umgebung ist bereits aktuell."
        return 0
    fi
    ensure_os_packages
    if [[ ! -x "${dest}/.venv/bin/python" ]]; then
        say "Creating the model server virtualenv…" "Erstelle die virtuelle Umgebung des Modellservers…"
        python3 -m venv "${dest}/.venv"
    fi
    local pip="${dest}/.venv/bin/python"
    say "Installing model server dependencies (${variant}). This can take several minutes." \
        "Installiere die Abhängigkeiten des Modellservers (${variant}). Das kann einige Minuten dauern."
    "$pip" -m pip install --upgrade pip
    case "$variant" in
        cpu|cuda|rocm)
            "$pip" -m pip install -r "${dest}/requirements-torch-${variant}.txt"
            ;;
        *)
            say "Unknown torch variant: $variant" "Unbekannte Torch-Variante: $variant"
            exit 1
            ;;
    esac
    "$pip" -m pip install -r "${dest}/requirements.txt"
    printf '%s\n' "$fingerprint" > "$stamp"
    printf '%s\n' "$variant" > "${dest}/.torch-variant"
}

write_unit() {
    local dest="$1"
    sed "s|__INSTALL_DIR__|${dest}|g" "${dest}/kittyhack-model-server.service" \
        > /etc/systemd/system/kittyhack-model-server.service
    systemctl daemon-reload
}

answer_is_yes() {
    case "${1,,}" in
        y|yes|j|ja) return 0 ;;
        *) return 1 ;;
    esac
}

amd_gpu_present() {
    if [[ -e /dev/kfd ]]; then
        return 0
    fi
    if [[ -d /sys/module/amdgpu ]]; then
        return 0
    fi
    if command -v lspci >/dev/null 2>&1 \
        && lspci -nn 2>/dev/null | grep -Eiq '(VGA compatible controller|3D controller|Display controller):.*(AMD/ATI|Advanced Micro Devices)'; then
        return 0
    fi
    return 1
}

choose_variant() {
    local dest="$1"
    if [[ -n "$TORCH_CHOICE" ]]; then
        printf '%s\n' "$TORCH_CHOICE"
        return
    fi
    local saved=""
    if [[ -f "${dest}/.torch-variant" ]]; then
        saved="$(tr -d '[:space:]' < "${dest}/.torch-variant")"
    fi
    if [[ "$NONINTERACTIVE" -eq 1 ]]; then
        if [[ -n "$saved" ]]; then
            printf '%s\n' "$saved"
        else
            echo cpu
        fi
        return
    fi
    # A finished CUDA or ROCm choice stays in place. A CPU install can still
    # switch to ROCm when an AMD GPU is present; kittyhack updates stay
    # noninteractive and do not ask.
    if [[ "$saved" == "cuda" || "$saved" == "rocm" ]]; then
        printf '%s\n' "$saved"
        return
    fi
    local answer=""
    if [[ -z "$saved" ]] && command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
        say \
            "An NVIDIA GPU is available. Install CUDA PyTorch as well? The download is several GB. CPU training works without it. (y/N): " \
            "Eine NVIDIA-GPU ist vorhanden. CUDA-PyTorch zusätzlich installieren? Der Download ist mehrere GB groß. Training auf der CPU funktioniert auch ohne. (y/N): "
        read -r -p "" answer || true
        if answer_is_yes "$answer"; then
            echo cuda
            return
        fi
    fi
    if amd_gpu_present; then
        local kfd_en="" kfd_de=""
        if [[ ! -e /dev/kfd ]]; then
            kfd_en=" The compute device /dev/kfd is not available, so training stays on the CPU until the AMD driver exposes it."
            kfd_de=" Das Compute-Gerät /dev/kfd ist nicht vorhanden, daher bleibt das Training auf der CPU, bis der AMD-Treiber es bereitstellt."
        fi
        if [[ "$saved" == "cpu" ]]; then
            say \
                "This model server is using the CPU build. An AMD GPU is available. Switch to ROCm PyTorch for that GPU? The download is several GB.${kfd_en} (y/N): " \
                "Dieser Modellserver nutzt die CPU-Variante. Eine AMD-GPU ist vorhanden. Auf ROCm-PyTorch für diese GPU wechseln? Der Download ist mehrere GB groß.${kfd_de} (y/N): "
        else
            say \
                "An AMD GPU is available. Install ROCm PyTorch for it? The download is several GB. CPU training works without it.${kfd_en} (y/N): " \
                "Eine AMD-GPU ist vorhanden. ROCm-PyTorch dafür installieren? Der Download ist mehrere GB groß. Training auf der CPU funktioniert auch ohne.${kfd_de} (y/N): "
        fi
        read -r -p "" answer || true
        if answer_is_yes "$answer"; then
            echo rocm
            return
        fi
    fi
    if [[ -n "$saved" ]]; then
        printf '%s\n' "$saved"
        return
    fi
    echo cpu
}

allow_private_port() {
    # The server listens on every interface. Ask before opening the port for
    # other computers on a home network. The public internet stays closed.
    local port="$1"
    local net answer="" backend=""
    if ! valid_port "$port"; then
        return 0
    fi
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        backend="ufw"
    elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
        backend="firewalld"
    else
        return 0
    fi
    if [[ "$NONINTERACTIVE" -eq 1 ]]; then
        say "The firewall is active. Port ${port} stays closed." \
            "Die Firewall ist aktiv. Port ${port} bleibt geschlossen."
        return 0
    fi
    say "Open port ${port} in the firewall for private networks? (y/N): " \
        "Port ${port} in der Firewall für private Netze öffnen? (y/N): "
    read -r -p "" answer || true
    if ! answer_is_yes "$answer"; then
        say "Leaving port ${port} closed." "Port ${port} bleibt geschlossen."
        return 0
    fi
    if [[ "$backend" == "ufw" ]]; then
        for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
            ufw allow from "$net" to any port "$port" proto tcp comment 'kittyhack model server' \
                || say "Could not open port ${port} in ufw for ${net}." \
                    "Port ${port} konnte in ufw für ${net} nicht geöffnet werden."
        done
        say "Allowed port ${port} from private networks (ufw)." \
            "Port ${port} ist für private Netze erlaubt (ufw)."
        return 0
    fi
    for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
        firewall-cmd --permanent \
            --add-rich-rule="rule family=ipv4 source address=${net} port port=${port} protocol=tcp accept" \
            >/dev/null || true
    done
    firewall-cmd --reload >/dev/null || true
    say "Allowed port ${port} from private networks (firewalld)." \
        "Port ${port} ist für private Netze erlaubt (firewalld)."
}

prepare_config() {
    local dest="$1"
    if [[ -f "${dest}/config.ini" ]]; then
        return 0
    fi
    cp -a "${dest}/config.ini.example" "${dest}/config.ini"
    local chosen="$PORT"
    if [[ -z "$chosen" && "$NONINTERACTIVE" -eq 0 ]]; then
        local entered=""
        say "Port for the model server [8000]: " "Port für den Modellserver [8000]: "
        read -r -p "" entered || true
        chosen="${entered:-8000}"
    fi
    chosen="${chosen:-8000}"
    if ! valid_port "$chosen"; then
        say "Invalid port: $chosen" "Ungültiger Port: $chosen"
        exit 1
    fi
    sed -i -E "s/^(port[[:space:]]*=[[:space:]]*).*/\\1${chosen}/" "${dest}/config.ini"
}

install_server() {
    assert_not_kittyflap
    if [[ -z "$SOURCES" || ! -d "${SOURCES}/model-server" ]]; then
        say "Pass --sources pointing at a Kittyhack checkout that contains model-server/." \
            "Gib --sources mit einem Kittyhack-Verzeichnis an, das model-server/ enthält."
        exit 1
    fi
    local dest
    dest="$(detect_install_dir)"
    if [[ "$dest" == "/" || "$dest" == "/root" || "$dest" == "$SOURCES" ]]; then
        say "Refusing install directory: $dest" "Installationsverzeichnis abgelehnt: $dest"
        exit 1
    fi
    say "Installing the model server to ${dest}" "Installiere den Modellserver nach ${dest}"
    copy_sources "$SOURCES" "$dest"
    prepare_config "$dest"
    local variant
    variant="$(choose_variant "$dest")"
    case "$variant" in
        cpu|cuda|rocm) ;;
        *)
            say "Unknown torch variant: $variant" "Unbekannte Torch-Variante: $variant"
            exit 1
            ;;
    esac
    ensure_venv "$dest" "$variant"
    write_unit "$dest"
    systemctl enable kittyhack-model-server.service
    systemctl restart kittyhack-model-server.service
    allow_private_port "$(read_port "${dest}/config.ini")"
    local port host
    port="$(read_port "${dest}/config.ini")"
    host="$(primary_ipv4)"
    say "Model server is running at http://${host}:${port}" "Modellserver läuft unter http://${host}:${port}"
    say "Training settings: ${dest}/config.ini" "Trainingseinstellungen: ${dest}/config.ini"
}

refresh_server() {
    if [[ ! -f /etc/systemd/system/kittyhack-model-server.service ]]; then
        return 0
    fi
    if [[ -z "$SOURCES" || ! -d "${SOURCES}/model-server" ]]; then
        say "Cannot refresh the model server: sources missing." "Modellserver kann nicht aktualisiert werden: Quellen fehlen."
        return 1
    fi
    local dest
    dest="$(detect_install_dir)"
    local was_active=0
    if systemctl is-active --quiet kittyhack-model-server.service; then
        was_active=1
    fi
    copy_sources "$SOURCES" "$dest"
    local variant="cpu"
    if [[ -f "${dest}/.torch-variant" ]]; then
        variant="$(tr -d '[:space:]' < "${dest}/.torch-variant")"
    fi
    case "$variant" in
        cpu|cuda|rocm) ;;
        *)
            say "Unknown torch variant: $variant" "Unbekannte Torch-Variante: $variant"
            return 1
            ;;
    esac
    ensure_venv "$dest" "$variant"
    write_unit "$dest"
    if [[ "$was_active" -eq 1 ]]; then
        systemctl restart kittyhack-model-server.service
    fi
    say "Model server code refreshed." "Modellserver-Code aktualisiert."
}

uninstall_server() {
    if [[ ! -f /etc/systemd/system/kittyhack-model-server.service ]]; then
        say "No model server service is installed." "Es ist kein Modellserver-Dienst installiert."
        return 0
    fi
    local dest
    dest="$(detect_install_dir)"
    if [[ "$ASSUME_YES" -ne 1 ]]; then
        local answer=""
        say \
            "Remove the model server, its service, and ${dest}? (y/N): " \
            "Modellserver, Dienst und ${dest} entfernen? (y/N): "
        read -r -p "" answer || true
        case "${answer,,}" in
            y|yes|j|ja) ;;
            *)
                say "Keeping the model server." "Modellserver bleibt installiert."
                return 0
                ;;
        esac
    fi
    systemctl disable --now kittyhack-model-server.service 2>/dev/null || true
    rm -f /etc/systemd/system/kittyhack-model-server.service
    systemctl daemon-reload || true
    if [[ -d "$dest" && "$dest" != "/" && "$dest" != "/root" ]]; then
        rm -rf "$dest"
    fi
    say "Model server removed." "Modellserver entfernt."
}

case "$COMMAND" in
    install) install_server ;;
    refresh) refresh_server ;;
    uninstall) uninstall_server ;;
    *) usage ;;
esac
