#!/usr/bin/env bash
# ensure_venv.sh — keep Kittyhack's .venv on the Python version required by this release.
#
# Modes:
#   --bootstrap  Fresh install: create/replace .venv in place (nothing should be using it).
#   --prepare    Update path: if mismatch, build .venv.new + install requirements (leave .venv alone).
#   --apply      ExecStartPre / reboot path: atomically swap .venv.new -> .venv, or repair if needed.
#
# Safety rules:
#   - Never delete .venv until a new venv has been created AND smoke-tested.
#   - --prepare must not swap while kittyhack/uvicorn may still be running.
#   - --apply is intended when this unit is not yet ExecStart'ed; on target boot,
#     kittyhack_control runs first (kittyhack is stopped), so a swap is safe.
#
# Required version is read from setup/REQUIRED_PYTHON (e.g. "3.11").
set -euo pipefail

MODE="--apply"
ROOT=""
SKIP_PIP=0
SMOKE_STRICT=0
REQUIREMENTS_FILE=""

usage() {
    cat <<'EOF'
Usage: ensure_venv.sh [--bootstrap|--prepare|--apply] [--root DIR] [--requirements FILE] [--skip-pip] [--smoke-strict]

  --bootstrap       Create .venv in place (fresh install / recovery with services stopped).
  --prepare         Build .venv.new if the active .venv Python mismatches REQUIRED_PYTHON.
  --apply           Swap .venv.new into place if present; otherwise repair on mismatch.
  --root DIR        Kittyhack install root (default: parent of setup/).
  --requirements F  requirements file (default: <root>/requirements.txt).
  --skip-pip        Skip pip install -r requirements.txt (bootstrap/testing only).
  --smoke-strict    Fail smoke test if torch/tflite_runtime cannot be imported.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bootstrap|--prepare|--apply) MODE="$1"; shift ;;
        --root) ROOT="${2:-}"; shift 2 ;;
        --requirements) REQUIREMENTS_FILE="${2:-}"; shift 2 ;;
        --skip-pip) SKIP_PIP=1; shift ;;
        --smoke-strict) SMOKE_STRICT=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$ROOT" ]]; then
    ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
fi

REQUIRED_FILE="${SCRIPT_DIR}/REQUIRED_PYTHON"
if [[ -z "$REQUIREMENTS_FILE" ]]; then
    REQUIREMENTS_FILE="${ROOT}/requirements.txt"
fi
VENV="${ROOT}/.venv"
VENV_NEW="${ROOT}/.venv.new"
VENV_OLD="${ROOT}/.venv.old"
LOCK_FILE="${ROOT}/.venv-ensure.lock"
PENDING_MARKER="${ROOT}/.runtime-update-pending"
STATUS_PID_FILE="${ROOT}/.update-status-server.pid"
STATUS_SERVER_SCRIPT="${SCRIPT_DIR}/update_status_server.py"
LOG_PREFIX="[ensure_venv]"
STATUS_SERVER_STARTED=0

log() { echo "${LOG_PREFIX} $*"; }
warn() { echo "${LOG_PREFIX} WARNING: $*" >&2; }
die() { echo "${LOG_PREFIX} ERROR: $*" >&2; exit 1; }

mark_update_pending() {
    touch "$PENDING_MARKER" 2>/dev/null || true
}

clear_update_pending() {
    rm -f "$PENDING_MARKER" 2>/dev/null || true
}

stop_status_server() {
    if [[ -f "$STATUS_PID_FILE" ]]; then
        local pid
        pid="$(tr -d '[:space:]' < "$STATUS_PID_FILE" 2>/dev/null || true)"
        if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
            log "Stopping update status page (pid ${pid})"
            kill "$pid" 2>/dev/null || true
            # Give it a moment; then force if needed.
            for _ in 1 2 3 4 5; do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.2
            done
            if kill -0 "$pid" 2>/dev/null; then
                kill -9 "$pid" 2>/dev/null || true
            fi
        fi
        rm -f "$STATUS_PID_FILE" 2>/dev/null || true
    fi
    STATUS_SERVER_STARTED=0
}

start_status_server() {
    # Best-effort only: a busy port 80 must not abort the real update work.
    if [[ ! -f "$STATUS_SERVER_SCRIPT" ]]; then
        warn "Status page script missing (${STATUS_SERVER_SCRIPT}); continuing without UI"
        return 0
    fi
    stop_status_server

    local py=""
    if command -v python3 >/dev/null 2>&1; then
        py="$(command -v python3)"
    elif [[ -x /usr/bin/python3 ]]; then
        py=/usr/bin/python3
    fi
    if [[ -z "$py" ]]; then
        warn "system python3 not found; continuing without update status page"
        return 0
    fi

    log "Starting update status page on port 80"
    # Detach from this shell so pip/swap output stays clean; logs go to journal via stdout.
    "$py" "$STATUS_SERVER_SCRIPT" --root "$ROOT" >>"${ROOT}/kittyhack.log" 2>&1 &
    local pid=$!
    echo "$pid" > "$STATUS_PID_FILE"
    STATUS_SERVER_STARTED=1
    sleep 0.4
    if ! kill -0 "$pid" 2>/dev/null; then
        warn "Update status page failed to stay up (port 80 busy?); continuing without UI"
        rm -f "$STATUS_PID_FILE" 2>/dev/null || true
        STATUS_SERVER_STARTED=0
        return 0
    fi
}

apply_needs_work() {
    if [[ -d "$VENV_NEW" ]]; then
        return 0
    fi
    if [[ -f "$PENDING_MARKER" ]]; then
        return 0
    fi
    if ! venv_matches_required "$VENV"; then
        return 0
    fi
    return 1
}

cleanup_on_exit() {
    # Always safe: no-op if the status page was never started.
    stop_status_server
}

read_required_python() {
    [[ -f "$REQUIRED_FILE" ]] || die "Missing ${REQUIRED_FILE}"
    local ver
    ver="$(tr -d '[:space:]' < "$REQUIRED_FILE")"
    [[ "$ver" =~ ^[0-9]+\.[0-9]+$ ]] || die "Invalid REQUIRED_PYTHON value: '${ver}'"
    echo "$ver"
}

REQUIRED_PYTHON="$(read_required_python)"

venv_python_mm() {
    local venv_dir="$1"
    local py="${venv_dir}/bin/python"
    if [[ ! -x "$py" ]]; then
        echo ""
        return 0
    fi
    "$py" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo ""
}

venv_matches_required() {
    local venv_dir="$1"
    local mm
    mm="$(venv_python_mm "$venv_dir")"
    [[ -n "$mm" && "$mm" == "$REQUIRED_PYTHON" ]]
}

ensure_uv_installed() {
    if command -v uv >/dev/null 2>&1; then
        return 0
    fi
    log "Installing uv (Python version manager)..."
    apt-get update -y >/dev/null 2>&1 || true
    apt-get install -y curl ca-certificates >/dev/null 2>&1 || true
    if curl -LsSf https://astral.sh/uv/install.sh | sh; then
        export PATH="${PATH}:/root/.local/bin:${HOME}/.local/bin"
    fi
    command -v uv >/dev/null 2>&1 || die "Failed to install uv; install python${REQUIRED_PYTHON} manually."
}

ensure_python_available() {
    local minor_cmd="python${REQUIRED_PYTHON}"
    if command -v "$minor_cmd" >/dev/null 2>&1; then
        return 0
    fi

    log "python${REQUIRED_PYTHON} not found; trying apt..."
    apt-get update -y >/dev/null 2>&1 || true
    if apt-get install -y \
        "python${REQUIRED_PYTHON}" \
        "python${REQUIRED_PYTHON}-venv" \
        "python${REQUIRED_PYTHON}-dev" >/dev/null 2>&1; then
        if command -v "$minor_cmd" >/dev/null 2>&1; then
            return 0
        fi
    fi

    log "apt python${REQUIRED_PYTHON} unavailable; falling back to uv-managed Python ${REQUIRED_PYTHON}..."
    ensure_uv_installed
    uv python install "$REQUIRED_PYTHON" || die "uv python install ${REQUIRED_PYTHON} failed"
}

create_venv_at() {
    local target="$1"
    local minor_cmd="python${REQUIRED_PYTHON}"

    rm -rf "$target"
    ensure_python_available

    if command -v "$minor_cmd" >/dev/null 2>&1; then
        log "Creating venv at ${target} with ${minor_cmd}"
        "$minor_cmd" -m venv "$target"
    else
        log "Creating venv at ${target} with uv (Python ${REQUIRED_PYTHON})"
        ensure_uv_installed
        uv venv --python "$REQUIRED_PYTHON" "$target"
    fi
    [[ -x "${target}/bin/python" ]] || die "venv python missing after create: ${target}"
}

pip_install_requirements() {
    local venv_dir="$1"
    if [[ "$SKIP_PIP" -eq 1 ]]; then
        log "Skipping pip install (--skip-pip)"
        return 0
    fi
    [[ -f "$REQUIREMENTS_FILE" ]] || die "Missing ${REQUIREMENTS_FILE}"

    # Force PyPI only. Raspberry Pi OS often enables piwheels via pip.conf /
    # PIP_EXTRA_INDEX_URL, which can mix incompatible wheels with pinned deps.
    export PIP_CONFIG_FILE=/dev/null
    export PIP_EXTRA_INDEX_URL=
    export PIP_INDEX_URL="https://pypi.org/simple"

    # shellcheck disable=SC1091
    source "${venv_dir}/bin/activate"
    pip install --index-url https://pypi.org/simple --timeout 120 --retries 10 --no-cache-dir -U pip setuptools wheel
    pip install --index-url https://pypi.org/simple --timeout 120 --retries 10 --no-cache-dir -r "$REQUIREMENTS_FILE"
    deactivate || true
}

purge_pip_cache() {
    # Free leftover wheel downloads from older installs/updates. Never fail the caller.
    log "Purging pip download cache (best-effort)..."
    if [[ -x "${VENV}/bin/pip" ]]; then
        "${VENV}/bin/pip" cache purge >/dev/null 2>&1 || true
    fi
    if [[ -x "${VENV_NEW}/bin/pip" ]]; then
        "${VENV_NEW}/bin/pip" cache purge >/dev/null 2>&1 || true
    fi
    if command -v pip3 >/dev/null 2>&1; then
        pip3 cache purge >/dev/null 2>&1 || true
    elif command -v pip >/dev/null 2>&1; then
        pip cache purge >/dev/null 2>&1 || true
    fi
    rm -rf /root/.cache/pip "${HOME:-/root}/.cache/pip" 2>/dev/null || true
}

smoke_test_venv() {
    local venv_dir="$1"
    local py="${venv_dir}/bin/python"
    [[ -x "$py" ]] || return 1

    # Always require the app entry stack.
    if ! "$py" -c 'import shiny, starlette, uvicorn, cv2, numpy' >/dev/null 2>&1; then
        warn "Smoke test failed: core imports (shiny/starlette/uvicorn/cv2/numpy)"
        return 1
    fi

    # ML stack is large; soft by default so a transient wheel glitch does not brick boot
    # after a successful core install. Use --smoke-strict for update-time prepare.
    if ! "$py" -c 'import torch' >/dev/null 2>&1; then
        if [[ "$SMOKE_STRICT" -eq 1 ]]; then
            warn "Smoke test failed: torch"
            return 1
        fi
        warn "torch import failed (non-strict); continuing"
    fi
    if ! "$py" -c 'import ai_edge_litert' >/dev/null 2>&1; then
        if [[ "$SMOKE_STRICT" -eq 1 ]]; then
            warn "Smoke test failed: ai_edge_litert"
            return 1
        fi
        warn "ai_edge_litert import failed (non-strict); continuing"
    fi
    return 0
}

rewrite_venv_paths_after_rename() {
    # venv console scripts embed absolute shebangs (e.g. #!/.../.venv.new/bin/python).
    # After `mv .venv.new .venv`, those shebangs break with systemd 203/EXEC
    # ("No such file or directory") even though `python -m …` still works.
    local venv_dir="$1"
    local old_prefix="$2"
    local new_prefix="$3"
    local bin_dir="${venv_dir}/bin"
    local rewritten=0

    [[ -d "$bin_dir" ]] || return 0
    [[ -n "$old_prefix" && -n "$new_prefix" && "$old_prefix" != "$new_prefix" ]] || return 0

    local f
    for f in "$bin_dir"/*; do
        [[ -e "$f" ]] || continue
        # Skip binaries / symlinks; only rewrite text entry-point scripts.
        if [[ -L "$f" || ! -f "$f" ]]; then
            continue
        fi
        if ! head -c 2 "$f" 2>/dev/null | grep -q '#!'; then
            continue
        fi
        if grep -Fq "$old_prefix" "$f" 2>/dev/null; then
            sed -i "s|${old_prefix}|${new_prefix}|g" "$f"
            rewritten=$((rewritten + 1))
        fi
    done

    if [[ -f "${venv_dir}/pyvenv.cfg" ]] && grep -Fq "$old_prefix" "${venv_dir}/pyvenv.cfg" 2>/dev/null; then
        sed -i "s|${old_prefix}|${new_prefix}|g" "${venv_dir}/pyvenv.cfg"
    fi

    if [[ "$rewritten" -gt 0 ]]; then
        log "Rewrote ${rewritten} venv script path(s) after rename (${old_prefix} -> ${new_prefix})"
    fi
}

atomic_swap_new_to_current() {
    [[ -d "$VENV_NEW" ]] || die "atomic_swap: ${VENV_NEW} missing"
    smoke_test_venv "$VENV_NEW" || die "atomic_swap: smoke test failed for ${VENV_NEW}"

    log "Swapping ${VENV_NEW} -> ${VENV} (previous kept as ${VENV_OLD})"
    rm -rf "$VENV_OLD"
    if [[ -d "$VENV" ]]; then
        mv "$VENV" "$VENV_OLD"
    fi
    mv "$VENV_NEW" "$VENV"
    rewrite_venv_paths_after_rename "$VENV" "$VENV_NEW" "$VENV"

    # Keep one previous venv for manual recovery; prune only if smoke of new current fails
    # (should not happen after tests above).
    if ! smoke_test_venv "$VENV"; then
        warn "Post-swap smoke failed; restoring ${VENV_OLD}"
        rm -rf "$VENV"
        mv "$VENV_OLD" "$VENV"
        die "Restored previous venv after failed post-swap smoke test"
    fi

    # Catch broken console-script shebangs early (systemd ExecStart used to call bin/uvicorn).
    if ! "${VENV}/bin/python" -m uvicorn --help >/dev/null 2>&1; then
        die "Post-swap check failed: python -m uvicorn does not run"
    fi

    log "Swap complete. Active Python: $(venv_python_mm "$VENV")"
}

with_lock() {
    # flock may be missing on minimal images; fall back to mkdir lock.
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$LOCK_FILE"
        flock -w 7200 9 || die "Could not acquire ${LOCK_FILE}"
        "$@"
        return $?
    fi
    local lockdir="${LOCK_FILE}.d"
    local waited=0
    while ! mkdir "$lockdir" 2>/dev/null; do
        sleep 1
        waited=$((waited + 1))
        [[ $waited -lt 7200 ]] || die "Could not acquire ${lockdir}"
    done
    # Preserve status-page cleanup if a nested die/exit happens.
    # shellcheck disable=SC2064
    trap "rmdir '$lockdir' 2>/dev/null || true; stop_status_server" EXIT
    "$@"
    local rc=$?
    rmdir "$lockdir" 2>/dev/null || true
    trap cleanup_on_exit EXIT
    return $rc
}

do_bootstrap() {
    log "bootstrap: required Python ${REQUIRED_PYTHON}"
    create_venv_at "$VENV"
    pip_install_requirements "$VENV"
    SMOKE_STRICT=1
    smoke_test_venv "$VENV" || die "bootstrap smoke test failed"
    rm -rf "$VENV_NEW" "$VENV_OLD"
    purge_pip_cache
    log "bootstrap OK ($(venv_python_mm "$VENV"))"
}

do_prepare() {
    log "prepare: required Python ${REQUIRED_PYTHON}"
    if venv_matches_required "$VENV"; then
        log "prepare: .venv already on Python $(venv_python_mm "$VENV"); nothing to do"
        # Stale .venv.new from a previous interrupted attempt — drop it if current is fine.
        if [[ -d "$VENV_NEW" ]]; then
            log "prepare: removing stale ${VENV_NEW}"
            rm -rf "$VENV_NEW"
        fi
        clear_update_pending
        # Only purge after a real prepare that may have left downloads; skip the
        # no-op match path so routine updates stay quiet when there is no cache work.
        return 0
    fi

    local current
    current="$(venv_python_mm "$VENV")"
    log "prepare: mismatch (have '${current:-none}', need '${REQUIRED_PYTHON}'); building ${VENV_NEW}"
    mark_update_pending
    # Drop the previous recovery copy before allocating another full venv.
    # Live recovery during prepare/apply is still the current .venv until swap.
    if [[ -d "$VENV_OLD" ]]; then
        log "prepare: removing previous ${VENV_OLD} to free disk space before creating ${VENV_NEW}"
        rm -rf "$VENV_OLD"
    fi
    create_venv_at "$VENV_NEW"
    pip_install_requirements "$VENV_NEW"
    SMOKE_STRICT=1
    if ! smoke_test_venv "$VENV_NEW"; then
        rm -rf "$VENV_NEW"
        clear_update_pending
        die "prepare smoke test failed; left existing .venv untouched"
    fi
    mark_update_pending
    purge_pip_cache
    log "prepare OK: ${VENV_NEW} ready for --apply on next service start/reboot"
}

do_apply() {
    log "apply: required Python ${REQUIRED_PYTHON}"

    local show_status=0
    if apply_needs_work; then
        show_status=1
        mark_update_pending
        start_status_server
    fi

    # Explicit cleanup (do not rely on EXIT traps — with_lock may also use them).
    local rc=0
    set +e
    _do_apply_work
    rc=$?
    set -e

    if [[ "$show_status" -eq 1 ]]; then
        stop_status_server
    fi
    return "$rc"
}

_do_apply_work() {
    if [[ -d "$VENV_NEW" ]]; then
        if smoke_test_venv "$VENV_NEW"; then
            atomic_swap_new_to_current
            clear_update_pending
            purge_pip_cache
            return 0
        fi
        warn "Found ${VENV_NEW} but smoke failed; removing and continuing"
        rm -rf "$VENV_NEW"
    fi

    if venv_matches_required "$VENV"; then
        log "apply: .venv already on Python $(venv_python_mm "$VENV"); OK"
        clear_update_pending
        return 0
    fi

    local current
    current="$(venv_python_mm "$VENV")"
    log "apply: mismatch (have '${current:-none}', need '${REQUIRED_PYTHON}'); repairing via ${VENV_NEW}"
    create_venv_at "$VENV_NEW"
    pip_install_requirements "$VENV_NEW"
    SMOKE_STRICT=1
    if ! smoke_test_venv "$VENV_NEW"; then
        rm -rf "$VENV_NEW"
        clear_update_pending
        die "apply repair failed; existing .venv left in place (may be wrong Python)"
    fi
    atomic_swap_new_to_current
    clear_update_pending
    purge_pip_cache
    return 0
}

run_mode() {
    case "$MODE" in
        --bootstrap) do_bootstrap ;;
        --prepare) do_prepare ;;
        --apply) do_apply ;;
        *) die "Unknown mode: ${MODE}" ;;
    esac
}

# Always tear down the status page on any exit (including die()).
trap cleanup_on_exit EXIT

with_lock run_mode
