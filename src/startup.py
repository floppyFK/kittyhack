"""Application boot: migrations, DB integrity, backend/background task start.

Call ``run()`` once from the server entrypoint (historical import-time boot).
"""
from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import threading
import time as tm
import uuid
from datetime import datetime, timedelta

from src.baseconfig import (
    CONFIG,
    AllowedToEnter,
    UserNotifications,
    get_loggable_config_value,
    remote_setup_required as check_remote_setup_required,
    save_config,
    set_language,
    update_single_config_parameter,
)
from src.helper import (
    SystemInfo,
    Versioning,
    check_and_stop_kittyflap_services,
    is_valid_uuid4,
    sigterm_monitor,
    wait_for_network,
)
from src.system import (
    KittyhackUpdater,
    LabelStudioInstall,
    WlanManager,
)
from src.database import (
    DatabaseCore,
    DbMigrations,
    EventsRepo,
    ReturnDataConfigDB,
)
from src.clock import monotonic_time
from src.mode import is_remote_mode
from src.paths import kittyhack_root
from src.model import RemoteModelTrainer
from src.backend.decisions import sync_pir_second_factor_suggestion
_ = set_language(CONFIG["LANGUAGE"])

git_version = "unknown"
version_from_changelog = False
git_repo_available = False
remote_setup_required = False
backend_thread = None
background_task_started = False
free_disk_space = 0.0
ids_with_original_blob: list = []

def _parse_version_tuple(version: str) -> tuple[int, ...]:
    parts = []
    for part in (version or "").split("."):
        if part.isdigit():
            parts.append(int(part))
        else:
            break
    return tuple(parts)

def _get_highest_changelog_version() -> str | None:
    changelog_dir = os.path.join(kittyhack_root(), "doc", "changelogs")
    pattern = os.path.join(changelog_dir, "changelog_v*_*.md")
    best_version = None
    best_tuple = None
    for path in glob.glob(pattern):
        name = os.path.basename(path)
        # Ignore beta changelogs for the fallback version (stable installs only).
        if "_beta_" in name:
            continue
        match = re.search(r"changelog_v(\d+(?:\.\d+)+)_", name)
        if not match:
            continue
        version = match.group(1)
        version_tuple = _parse_version_tuple(version)
        if not version_tuple:
            continue
        if best_tuple is None or version_tuple > best_tuple:
            best_tuple = version_tuple
            best_version = version
    return best_version

def _get_fallback_version() -> str:
    version = _get_highest_changelog_version()
    if version:
        return f"v{version}"
    return "unknown"

def start_backend_if_needed():
    """Start the backend loop thread once (idempotent)."""
    global backend_thread
    if backend_thread is not None and backend_thread.is_alive():
        return
    logging.info("Starting backend...")
    from src.backend import backend_main
    from src.runtime_flags import is_simulate_mode
    backend_thread = threading.Thread(target=backend_main, args=(is_simulate_mode(),), daemon=True)
    backend_thread.start()

def start_background_task_if_needed():
    """Start the frontend periodic background scheduler once."""
    global background_task_started
    if background_task_started:
        return
    start_background_task()
    background_task_started = True

def prune_old_backups(backup_dir: str, keep: int = 3):
    """Keep only the newest ``keep`` kittyhack_backup_*.db files."""
    try:
        pattern = os.path.join(backup_dir, "kittyhack_backup_*.db")
        files = [f for f in glob.glob(pattern) if os.path.isfile(f)]
        if len(files) <= keep:
            return
        # Sort by modification time (newest first)
        files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        to_delete = files[keep:]
        for f in to_delete:
            try:
                os.remove(f)
                logging.info(f"[DATABASE_BACKUP] Pruned old backup: {f}")
            except Exception as e:
                logging.warning(f"[DATABASE_BACKUP] Failed to delete old backup '{f}': {e}")
    except Exception as e:
        logging.warning(f"[DATABASE_BACKUP] Failed pruning backups: {e}")

def start_background_task():
    """Spawn the periodic frontend background-task thread."""
    # Register task in the sigterm_monitor object
    sigterm_monitor.register_task()

    def run_periodically():
        last_periodic_jobs_run_mono = monotonic_time()  # Start the first periodic jobs in PERIODIC_JOBS_INTERVAL seconds

        # Model training status check cadence (seconds)
        last_model_training_check_mono = monotonic_time() - 120  # allow immediate first check after boot

        # --- Added migration control state ---
        migration_last_mono = monotonic_time() - 5  # allow immediate first run
        migration_in_progress = False
        migration_batch_size = 200
        global ids_with_original_blob  # reuse list defined at startup

        # --- APT updates cadence state (24h) ---
        last_apt_update_mono = monotonic_time() - 86400  # allow immediate first check on boot

        while not sigterm_monitor.stop_now:
            # --- Background legacy image migration (every >=5s) ---
            # Migrate original_image / thumbnail BLOBs batch-wise (max 100 IDs) to filesystem
            try:
                if (not migration_in_progress
                    and ids_with_original_blob
                    and (monotonic_time() - migration_last_mono) >= 5):
                    migration_in_progress = True
                    batch = ids_with_original_blob[:migration_batch_size]
                    logging.info(f"[BG_MIGRATION] Starting migration batch of {len(batch)} IDs (remaining total: {len(ids_with_original_blob)})")
                    result = EventsRepo.perform_event_image_migration_ids(
                        CONFIG['KITTYHACK_DATABASE_PATH'],
                        batch,
                        chunk_size=migration_batch_size
                    )
                    if result.success:
                        # Remove migrated IDs from list
                        migrated_set = set(batch)
                        ids_with_original_blob = [i for i in ids_with_original_blob if i not in migrated_set]
                        logging.info(f"[BG_MIGRATION] Batch done. Remaining legacy IDs: {len(ids_with_original_blob)} | {result.message}")
                        if not ids_with_original_blob:
                            # Perform a single VACUUM at the very end to reclaim space
                            logging.info("[BG_MIGRATION] Running final VACUUM on database after full migration...")
                            DatabaseCore.vacuum_database(CONFIG['KITTYHACK_DATABASE_PATH'])
                            logging.info("[BG_MIGRATION] Final VACUUM completed.")
                            CONFIG['EVENT_IMAGES_FS_MIGRATED'] = True
                            logging.info("[BG_MIGRATION] All legacy image blobs migrated successfully.")
                    else:
                        logging.warning(f"[BG_MIGRATION] Batch migration failed: {result.message}")
                    migration_last_mono = monotonic_time()
                    migration_in_progress = False
            except Exception as e:
                logging.error(f"[BG_MIGRATION] Unexpected migration error: {e}")
                migration_in_progress = False
                migration_last_mono = monotonic_time()

            # --- Model training status polling (every 120s, only if a training is active) ---
            now_mono = monotonic_time()
            if (now_mono - last_model_training_check_mono) >= 120:
                last_model_training_check_mono = now_mono
                try:
                    if is_valid_uuid4(CONFIG.get("MODEL_TRAINING", "")):
                        # Important: do not emit UI notifications from the background thread.
                        RemoteModelTrainer.check_model_training_result(show_notification=False, show_in_progress=False)
                except Exception as e:
                    logging.warning(f"[MODEL_TRAINING] Periodic status check failed: {e}")

            # --- Main periodic jobs (every PERIODIC_JOBS_INTERVAL seconds) ---
            # Periodically check that the kwork and manager services are NOT running anymore
            now_mono = monotonic_time()
            if now_mono - last_periodic_jobs_run_mono >= CONFIG['PERIODIC_JOBS_INTERVAL']:
                last_periodic_jobs_run_mono = now_mono
                from src.runtime_flags import is_simulate_mode
                check_and_stop_kittyflap_services(is_simulate_mode())
                immediate_bg_task("background task")

                # --- Base-system APT updates (once per 24h, only at night between 2:00 and 4:00) ---
                try:
                    hours_since_last_apt = (monotonic_time() - last_apt_update_mono) / 3600.0
                    current_time = datetime.now()
                    in_night_window = 2 <= current_time.hour < 4

                    if hours_since_last_apt >= 24:
                        if in_night_window:
                            logging.info("[APT] Starting base-system package refresh and upgrades...")
                            ok, msg = KittyhackUpdater.upgrade_base_system_packages()
                            if ok:
                                logging.info(f"[APT] Upgrade finished successfully: {msg}")
                            else:
                                logging.error(f"[APT] Upgrade failed: {msg}")
                            # Update timestamp regardless to avoid retry storms
                            last_apt_update_mono = monotonic_time()
                except Exception as e:
                    logging.error(f"[APT] Unexpected error in periodic APT update: {e}")
                    last_apt_update_mono = monotonic_time()

                # Cleanup the events table
                EventsRepo.cleanup_deleted_events(CONFIG['KITTYHACK_DATABASE_PATH'])
                ids_without_thumbnail = EventsRepo.get_ids_without_thumbnail(CONFIG['KITTYHACK_DATABASE_PATH'])
                if ids_without_thumbnail:
                    # Limit the number of thumbnails to generate in one run to 200 to avoid high CPU load
                    ids_without_thumbnail.reverse()
                    thumbnails_to_process = ids_without_thumbnail[:200]
                    logging.info(f"[TRIGGER: background task] Start generating thumbnails for {len(thumbnails_to_process)} events (out of {len(ids_without_thumbnail)} total)...")
                    for id in thumbnails_to_process:
                        EventsRepo.get_thubmnail_by_id(database=CONFIG['KITTYHACK_DATABASE_PATH'], photo_id=id)
                    logging.info(f"[DATABASE] Generated {len(thumbnails_to_process)} thumbnails for events without thumbnail.")
                else:
                    logging.info("[TRIGGER: background task] No events found without thumbnail.")

                # Check the free disk space
                free_disk_space = SystemInfo.get_free_disk_space()

                # Check the latest version of kittyhack on GitHub, if the periodic version check is enabled
                if CONFIG['PERIODIC_VERSION_CHECK']:
                    CONFIG['LATEST_VERSION'] = Versioning.read_latest_kittyhack_version()

                # Check if the last backup date is stored in the configuration
                if CONFIG['LAST_DB_BACKUP_DATE']:
                    last_backup_date = datetime.strptime(CONFIG['LAST_DB_BACKUP_DATE'], '%Y-%m-%d %H:%M:%S')
                else:
                    last_backup_date = datetime.min

                # Check if the last scheduled vacuum date is stored in the configuration
                if CONFIG['LAST_VACUUM_DATE']:
                    last_vacuum_date = datetime.strptime(CONFIG['LAST_VACUUM_DATE'], '%Y-%m-%d %H:%M:%S')
                else:
                    last_vacuum_date = datetime.min

                # Perform backup only between 2:00 and 4:00 AM if last backup is >22h old
                current_time = datetime.now()
                backup_window = 2 <= current_time.hour < 4
                backup_needed = (current_time - last_backup_date) > timedelta(hours=22)
                
                if backup_needed and backup_window:
                    if CONFIG.get('EVENT_IMAGES_FS_MIGRATED', False):
                        logging.info(f"[TRIGGER: background task] It is {current_time.hour}:{current_time.minute}:{current_time.second}. Start backup of the kittyhack database...")
                        # Write timestamped backup next to the main DB
                        backup_dir = os.path.dirname(CONFIG['KITTYHACK_DATABASE_PATH']) or "."
                        backup_name = f"kittyhack_backup_{current_time.strftime('%Y%m%d_%H%M%S')}.db"
                        backup_dest = os.path.join(backup_dir, backup_name)
                        result = DatabaseCore.backup_database_sqlite(CONFIG['KITTYHACK_DATABASE_PATH'], backup_dest)
                        if result.success:
                            CONFIG['LAST_DB_BACKUP_DATE'] = current_time.strftime('%Y-%m-%d %H:%M:%S')
                            update_single_config_parameter("LAST_DB_BACKUP_DATE")
                            logging.info(f"[DATABASE_BACKUP] Backup successful: {backup_dest}")
                            prune_old_backups(backup_dir, keep=3)
                        else:
                            logging.error(f"[DATABASE_BACKUP] Backup failed: {result.message}")
                    else:
                        logging.info("[DATABASE_BACKUP] Skipping backup: legacy image migration not finished (EVENT_IMAGES_FS_MIGRATED = False).")

                # Perform Scheduled VACUUM only if the last scheduled vacuum date is older than 24 hours
                if (datetime.now() - last_vacuum_date) > timedelta(days=1):
                    logging.info("[TRIGGER: background task] Start cleanup of orphan image files...")
                    EventsRepo.cleanup_orphan_image_files(CONFIG['KITTYHACK_DATABASE_PATH'])
                    logging.info("[TRIGGER: background task] Start VACUUM of the kittyhack database...")
                    DatabaseCore.vacuum_database(CONFIG['KITTYHACK_DATABASE_PATH'])
                    CONFIG['LAST_VACUUM_DATE'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    update_single_config_parameter("LAST_VACUUM_DATE")

                # Log system information
                SystemInfo.log_system_information()

            # Sleep 5 seconds (split to allow faster shutdown)
            for __ in range(5):
                if sigterm_monitor.stop_now:
                    break
                tm.sleep(1.0)
        
        logging.info("[TRIGGER: background task] Stopped background task scheduler.")
        sigterm_monitor.signal_task_done()

    frontend_bg_thread = threading.Thread(target=run_periodically, daemon=True)
    frontend_bg_thread.start()

def immediate_bg_task(trigger = "reload"):
    """Hook for an immediate background pass (currently a no-op placeholder)."""
    logging.info(f"[TRIGGER: {trigger}] Start immediate background task")
    # TODO: immediate background task
    logging.info(f"[TRIGGER: {trigger}] Currently nothing to do here - keep for future usage")
    logging.info(f"[TRIGGER: {trigger}] End immediate background task")

def _ensure_valid_startup_model_selection() -> None:
    """Ensure configured model exists, otherwise fallback and notify user."""
    from src.model import YoloModel

    configured_yolo = (CONFIG.get('YOLO_MODEL') or '').strip()
    if not configured_yolo:
        return

    try:
        configured_path = YoloModel.get_model_path(configured_yolo)
    except Exception as e:
        logging.warning(f"[MODEL] Failed to validate configured YOLO model '{configured_yolo}': {e}")
        configured_path = None

    if configured_path:
        return

    logging.warning(
        f"[MODEL] Configured YOLO model '{configured_yolo}' does not exist. Selecting fallback model."
    )

    fallback_yolo_id = None
    fallback_yolo_name = ""
    try:
        available_models = [m for m in YoloModel.get_model_list() if (m.get('unique_id') or '').strip()]
        available_models.sort(key=lambda m: (m.get('creation_date') or ''), reverse=True)
        for model in available_models:
            candidate_id = (model.get('unique_id') or '').strip()
            if not candidate_id:
                continue
            if candidate_id == configured_yolo:
                continue
            if YoloModel.get_model_path(candidate_id):
                fallback_yolo_id = candidate_id
                fallback_yolo_name = str(model.get('full_display_name') or model.get('display_name') or candidate_id)
                break
    except Exception as e:
        logging.warning(f"[MODEL] Failed while searching fallback YOLO model: {e}")

    if fallback_yolo_id:
        CONFIG['YOLO_MODEL'] = fallback_yolo_id
        CONFIG['TFLITE_MODEL_VERSION'] = ""
        update_single_config_parameter("YOLO_MODEL")
        update_single_config_parameter("TFLITE_MODEL_VERSION")
        try:
            UserNotifications.add(
                header=_("Model fallback applied"),
                message=(
                    _("The configured YOLO model was not found at startup:") + f" {configured_yolo}\n\n" +
                    _("Kittyhack switched automatically to another available YOLO model:") + f" {fallback_yolo_name}"
                ),
                type="warning",
                id="model_fallback_missing_yolo",
                skip_if_id_exists=True,
            )
        except Exception as e:
            logging.warning(f"[MODEL] Failed to create user notification for YOLO fallback: {e}")
        logging.info(f"[MODEL] Fallback applied: YOLO_MODEL={fallback_yolo_id}")
        return

    CONFIG['YOLO_MODEL'] = ""
    CONFIG['TFLITE_MODEL_VERSION'] = "original_kittyflap_model_v2"
    update_single_config_parameter("YOLO_MODEL")
    update_single_config_parameter("TFLITE_MODEL_VERSION")
    try:
        UserNotifications.add(
            header=_("Model fallback applied"),
            message=(
                _("The configured YOLO model was not found at startup:") + f" {configured_yolo}\n\n" +
                _("No other YOLO model was available. Kittyhack switched to the shipped TFLite model:") +
                " original_kittyflap_model_v2"
            ),
            type="warning",
            id="model_fallback_missing_yolo",
            skip_if_id_exists=True,
        )
    except Exception as e:
        logging.warning(f"[MODEL] Failed to create user notification for TFLite fallback: {e}")
    logging.info("[MODEL] Fallback applied: TFLITE_MODEL_VERSION=original_kittyflap_model_v2")

def run() -> None:
    """Execute the historical import-time boot sequence."""
    global git_version, version_from_changelog, git_repo_available, remote_setup_required, free_disk_space, ids_with_original_blob

    logging.info("----- Startup -----------------------------------------------------------------------------------------")

    if CONFIG['STARTUP_SHUTDOWN_FLAG'] == True:
        logging.warning("!!!!!!!!!! STARTUP FLAG WAS ACTIVE - NOT GRACEFUL SHUTDOWN DETECTED !!!!!!!!!!")
        CONFIG['NOT_GRACEFUL_SHUTDOWNS'] = CONFIG['NOT_GRACEFUL_SHUTDOWNS'] + 1
    else:
        CONFIG['NOT_GRACEFUL_SHUTDOWNS'] = 0
    CONFIG['STARTUP_SHUTDOWN_FLAG'] = True
    update_single_config_parameter("NOT_GRACEFUL_SHUTDOWNS")
    update_single_config_parameter("STARTUP_SHUTDOWN_FLAG")

    if CONFIG['NOT_GRACEFUL_SHUTDOWNS'] >= 3:
        logging.error("Not graceful shutdown detected 3 times in a row!")
        CONFIG['NOT_GRACEFUL_SHUTDOWNS'] = 0
        update_single_config_parameter("NOT_GRACEFUL_SHUTDOWNS")

        # Add a entry to the user notifications, which will be shown at the next login in the frontend
        shutdown_message = (
            _("The kittyflap was not shut down gracefully several times in a row. Please do not power off the device without shutting it down first, otherwise the database may be corrupted!") + "\n\n" +
            _("If you have shut it down gracefully and see this message, please report it in the") + " " +
            "[GitHub issue tracker](https://github.com/floppyFK/kittyhack/issues), " +
            _("thanks!")
        )

        UserNotifications.add(
            header=_("Several crashes detected!"),
            message=shutdown_message,
                    type="warning",
                    id="not_graceful_shutdown",
                    skip_if_id_exists=True
            )

    if not CONFIG['MQTT_DEVICE_ID']:
        # Generate a new MQTT device ID if it does not exist
        CONFIG['MQTT_DEVICE_ID'] = f"kittyhack_{str(uuid.uuid4()).split('-')[0]}"
        update_single_config_parameter('MQTT_DEVICE_ID')
        logging.info(f"[BACKEND] Generated MQTT device ID: {CONFIG['MQTT_DEVICE_ID']}")

    # Image-processing core policy: all CPUs in remote-mode, always 1 core on Kittyflap hardware.
    if is_remote_mode() and not CONFIG.get('USE_ALL_CORES_FOR_IMAGE_PROCESSING', False):
        logging.info("[REMOTE_MODE] Enforcing USE_ALL_CORES_FOR_IMAGE_PROCESSING=True.")
        CONFIG['USE_ALL_CORES_FOR_IMAGE_PROCESSING'] = True
        update_single_config_parameter("USE_ALL_CORES_FOR_IMAGE_PROCESSING")
    elif (not is_remote_mode()) and CONFIG.get('USE_ALL_CORES_FOR_IMAGE_PROCESSING', False):
        logging.info("[TARGET] Disabling USE_ALL_CORES_FOR_IMAGE_PROCESSING (unsupported on Kittyflap hardware).")
        CONFIG['USE_ALL_CORES_FOR_IMAGE_PROCESSING'] = False
        update_single_config_parameter("USE_ALL_CORES_FOR_IMAGE_PROCESSING")

    _ensure_valid_startup_model_selection()

    git_repo_available = os.path.isdir(os.path.join(kittyhack_root(), ".git"))
    version_from_changelog = False
    try:
        git_version = Versioning.get_git_version()
    except Exception as e:
        logging.warning(f"Failed to read git version: {e}")
        git_version = "unknown"

    if not git_version or git_version == "unknown":
        fallback_version = _get_fallback_version()
        if fallback_version != "unknown":
            git_version = fallback_version
            version_from_changelog = True
            logging.warning(f"Using version from changelog files: {git_version}")

    remote_setup_required = check_remote_setup_required()

    # Now update the last booted version in the configuration
    CONFIG['LAST_BOOTED_VERSION'] = git_version
    update_single_config_parameter("LAST_BOOTED_VERSION")

    # Check if the CAT_THRESHOLD setting is lower than the MIN_THRESHOLD. If so, set it to the MIN_THRESHOLD
    if CONFIG['CAT_THRESHOLD'] < CONFIG['MIN_THRESHOLD']:
        logging.warning(f"CAT_THRESHOLD is lower than MIN_THRESHOLD ({CONFIG['MIN_THRESHOLD']}). Setting CAT_THRESHOLD to MIN_THRESHOLD.")
        CONFIG['CAT_THRESHOLD'] = CONFIG['MIN_THRESHOLD']
        update_single_config_parameter("CAT_THRESHOLD")

    # END OF MIGRATION RULES ############################################################################################

    logging.info(f"Current version: {git_version}")

    # Log all configuration values from CONFIG dictionary
    logging.info("Configuration values:")
    for key, value in CONFIG.items():
        loggable_value = get_loggable_config_value(key, value)
        logging.info(f"{key}={loggable_value}")

    # IMPORTANT: First of all check that the kwork and manager services are NOT running
    # (only relevant on the target device)
    if not is_remote_mode():
        from src.runtime_flags import is_simulate_mode
        check_and_stop_kittyflap_services(is_simulate_mode())

        # Migration: devices updating from pre-remote versions may still have kittyhack.service enabled
        # and kittyhack_control.service disabled/missing. Ensure the new boot semantics take effect
        # on the next reboot (best-effort; should never block startup).
        try:
            KittyhackUpdater.ensure_target_boot_service_semantics()
        except Exception as e:
            logging.warning(f"[SYSTEM] Failed to ensure target boot service semantics: {e}")

    # Remote-mode constraints
    if is_remote_mode():
        if not (CONFIG.get('REMOTE_TARGET_HOST') or '').strip():
            logging.warning("[REMOTE_MODE] REMOTE_TARGET_HOST is empty; remote sensors/actors will not connect.")

    # Cleanup old temp files
    if os.path.exists("/tmp/kittyhack.db"):
        try:
            os.remove("/tmp/kittyhack.db")
        except:
            logging.error("Failed to delete the temporary kittyhack.db file.")

    # Remove deprecated backup database file (pre v2.4) at startup
    if os.path.exists(CONFIG['KITTYHACK_DATABASE_BACKUP_PATH']):
        try:
            os.remove(CONFIG['KITTYHACK_DATABASE_BACKUP_PATH'])
            logging.info(f"Deleted deprecated backup database file: {CONFIG['KITTYHACK_DATABASE_BACKUP_PATH']}")
        except Exception as e:
            logging.warning(f"Failed to delete deprecated backup database file '{CONFIG['KITTYHACK_DATABASE_BACKUP_PATH']}': {e}")

    try:
        prune_old_backups(os.path.dirname(CONFIG['KITTYHACK_DATABASE_PATH']) or ".")
    except Exception:
        pass

    # Initial database integrity check
    if os.path.exists(CONFIG['KITTYHACK_DATABASE_PATH']):
        db_check = DatabaseCore.check_database_integrity(CONFIG['KITTYHACK_DATABASE_PATH'])
        if db_check.success:
            logging.info("Initial Database integrity check successful.")
        else:
            logging.error(f"Initial Database integrity check failed: {db_check.message}")
            backup_dir = os.path.dirname(CONFIG['KITTYHACK_DATABASE_PATH']) or "."
            pattern = os.path.join(backup_dir, "kittyhack_backup_*.db")
            backup_files = [f for f in glob.glob(pattern) if os.path.isfile(f)]
            if backup_files:
                # Newest first
                backup_files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
                latest_backup = backup_files[0]
                logging.info(f"[DATABASE_BACKUP] Attempting restore from latest backup: {latest_backup}")
                try:
                    # Preserve corrupted file
                    corrupt_archive = CONFIG['KITTYHACK_DATABASE_PATH'] + f".corrupt_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
                    try:
                        shutil.move(CONFIG['KITTYHACK_DATABASE_PATH'], corrupt_archive)
                        logging.info(f"Corrupted database archived as: {corrupt_archive}")
                    except Exception as e:
                        logging.warning(f"Failed to archive corrupted database: {e}")
                    shutil.copy2(latest_backup, CONFIG['KITTYHACK_DATABASE_PATH'])
                    # Re-check integrity after restore
                    post_restore = DatabaseCore.check_database_integrity(CONFIG['KITTYHACK_DATABASE_PATH'])
                    if post_restore.success:
                        logging.info(f"[DATABASE_BACKUP] Restore successful from {latest_backup}")
                        # --- User notification about successful restore ---
                        try:
                            UserNotifications.add(
                                header=_("Database restored"),
                                message=_("The kittyhack database was corrupted at startup and has been restored from backup: {}. The corrupted file was archived as: {}").format(latest_backup, corrupt_archive),
                                type="warning",
                                id="db_restored",
                                skip_if_id_exists=True
                            )
                        except Exception as e:
                            logging.warning(f"Failed to create user notification for DB restore: {e}")
                    else:
                        logging.error(f"[DATABASE_BACKUP] Restore failed: {post_restore.message}")
                except Exception as e:
                    logging.error(f"[DATABASE_BACKUP] Unexpected error during restore: {e}")
            else:
                logging.error("[DATABASE_BACKUP] No backup files (kittyhack_backup_*.db) found. Remove corrupted database and start fresh.")
                os.remove(CONFIG['KITTYHACK_DATABASE_PATH'])
    else:
        logging.warning(f"Database '{CONFIG['KITTYHACK_DATABASE_PATH']}' not found. This is probably the first start of the application.")

    # Check, if the kittyhack database file exists. If not, create it.
    if not os.path.exists(CONFIG['KITTYHACK_DATABASE_PATH']):
        logging.info(f"Database '{CONFIG['KITTYHACK_DATABASE_PATH']}' not found. Creating it...")
        DbMigrations.create_kittyhack_events_table(CONFIG['KITTYHACK_DATABASE_PATH'])

    if not DatabaseCore.check_if_table_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "events"):
        logging.warning(f"Table 'events' not found in the kittyhack database. Creating it...")
        DbMigrations.create_kittyhack_events_table(CONFIG['KITTYHACK_DATABASE_PATH'])

    # v1.5.1: Check if the "thumbnails" column exists in the "events" table. If not, add it
    if not DatabaseCore.check_if_column_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "thumbnail"):
        logging.warning(f"Column 'thumbnail' not found in the 'events' table. Adding it...")
        DatabaseCore.add_column_to_table(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "thumbnail", "BLOB")

    # v2.0.0: Check if the "own_cat_probability" column exists in the "events" table. If not, add it
    if not DatabaseCore.check_if_column_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "own_cat_probability"):
        logging.warning(f"Column 'own_cat_probability' not found in the 'events' table. Adding it...")
        DatabaseCore.add_column_to_table(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "own_cat_probability", "REAL")

    # v2.5.0: Store recorded image dimensions for better initial aspect-ratio in the event modal
    if not DatabaseCore.check_if_column_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "img_width"):
        logging.warning("Column 'img_width' not found in the 'events' table. Adding it...")
        DatabaseCore.add_column_to_table(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "img_width", "INTEGER")
    if not DatabaseCore.check_if_column_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "img_height"):
        logging.warning("Column 'img_height' not found in the 'events' table. Adding it...")
        DatabaseCore.add_column_to_table(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "img_height", "INTEGER")

    # v2.5.0: Store effective FPS per event so playback speed matches capture speed
    if not DatabaseCore.check_if_column_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "effective_fps"):
        logging.warning("Column 'effective_fps' not found in the 'events' table. Adding it...")
        DatabaseCore.add_column_to_table(CONFIG['KITTYHACK_DATABASE_PATH'], "events", "effective_fps", "REAL")

    if not DatabaseCore.check_if_table_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "motion_timeline"):
        logging.warning("Table 'motion_timeline' not found in the kittyhack database. Creating it...")
        DbMigrations.create_motion_timeline_table(CONFIG['KITTYHACK_DATABASE_PATH'])

    DbMigrations.create_visit_stats_tables(CONFIG['KITTYHACK_DATABASE_PATH'])

    if not DatabaseCore.check_if_table_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "photo"):
        logging.warning(f"Legacy table 'photo' not found in the kittyhack database. Creating it...")
        DbMigrations.create_kittyhack_photo_table(CONFIG['KITTYHACK_DATABASE_PATH'])

    # Check if table "cats" exist in the kittyhack database. If not, create it.
    if not DatabaseCore.check_if_table_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "cats"):
        logging.warning(f"Table 'cats' not found in the kittyhack database. Creating it...")
        DbMigrations.create_kittyhack_cats_table(CONFIG['KITTYHACK_DATABASE_PATH'])
        # Migrate the cats from the kittyflap database to the kittyhack database
        if DatabaseCore.check_if_table_exists(CONFIG['DATABASE_PATH'], "cat"):
            DbMigrations.migrate_cats_to_kittyhack(kittyflap_db=CONFIG['DATABASE_PATH'], kittyhack_db=CONFIG['KITTYHACK_DATABASE_PATH'])
        else:
            logging.warning("Table 'cat' not found in the kittyflap database. No cats migrated to the kittyhack database.")

    # v3.4.0: Ensure per-cat settings columns exist (enable_prey_detection, allow_entry, allow_exit)
    for db in [CONFIG['KITTYHACK_DATABASE_PATH']]:
        try:
            if os.path.exists(db):
                if not DatabaseCore.check_if_column_exists(db, "cats", "enable_prey_detection"):
                    logging.warning(f"Column 'enable_prey_detection' not found in the 'cats' table of {db}. Adding it...")
                    DatabaseCore.add_column_to_table(db, "cats", "enable_prey_detection", "INTEGER DEFAULT 1")
                    DatabaseCore.write_stmt_to_database(db, "UPDATE cats SET enable_prey_detection = 1 WHERE enable_prey_detection IS NULL")
                if not DatabaseCore.check_if_column_exists(db, "cats", "allow_entry"):
                    logging.warning(f"Column 'allow_entry' not found in the 'cats' table of {db}. Adding it...")
                    DatabaseCore.add_column_to_table(db, "cats", "allow_entry", "INTEGER DEFAULT 1")
                    DatabaseCore.write_stmt_to_database(db, "UPDATE cats SET allow_entry = 1 WHERE allow_entry IS NULL")
                if not DatabaseCore.check_if_column_exists(db, "cats", "allow_exit"):
                    logging.warning(f"Column 'allow_exit' not found in the 'cats' table of {db}. Adding it...")
                    DatabaseCore.add_column_to_table(db, "cats", "allow_exit", "INTEGER DEFAULT 1")
                    DatabaseCore.write_stmt_to_database(db, "UPDATE cats SET allow_exit = 1 WHERE allow_exit IS NULL")
        except Exception as e:
            logging.error(f"Failed to ensure per-cat settings columns in database {db}: {e}")

    if DatabaseCore.check_if_table_exists(CONFIG['KITTYHACK_DATABASE_PATH'], "photo"):
        logging.info("Table 'photo' found in the kittyhack database. Migrating it to 'events'...")
        DbMigrations.migrate_photos_to_events(CONFIG['KITTYHACK_DATABASE_PATH'])

    # Migrate the kittyflap config database table into the config.ini:
    if DatabaseCore.check_if_table_exists(CONFIG['DATABASE_PATH'], "config") and CONFIG['KITTYFLAP_CONFIG_MIGRATED'] == False:
        logging.info("Table 'config' found in the kittyflap database. Migrating it to the config.ini...")
        df_config = DatabaseCore.db_get_config(CONFIG['DATABASE_PATH'], ReturnDataConfigDB.all)
        if not df_config.empty:
            CONFIG['MOUSE_CHECK_ENABLED'] = bool(df_config.iloc[0]["detect_prey"])
            CONFIG['ALLOWED_TO_ENTER'] = AllowedToEnter.ALL if bool(df_config.iloc[0]["accept_all_cats"]) else AllowedToEnter.KNOWN
            CONFIG['KITTYFLAP_CONFIG_MIGRATED'] = True
            if save_config():
                logging.info("Kittyflap configuration migrated successfully.")
            else:
                logging.error("Failed to save the migrated kittyflap configuration.")
        else:
            logging.error("Failed to read the configuration from the kittyflap database.")

    # Create indexes for the kittyhack database
    DatabaseCore.create_index_on_events(CONFIG['KITTYHACK_DATABASE_PATH'])

    try:
        from src.statistics import backfill_from_events
        backfill_from_events(CONFIG['KITTYHACK_DATABASE_PATH'])
    except Exception as e:
        logging.error(f"[VISIT_STATS] Backfill failed: {e}")

    # Wait for internet connectivity and NTP sync
    logging.info("Waiting for network connectivity...")
    if wait_for_network(timeout=10):
        try:
            CONFIG['LATEST_VERSION'] = Versioning.read_latest_kittyhack_version(timeout=3)
            logging.info(f"[VERSION] Latest Kittyhack version fetched at startup: {CONFIG['LATEST_VERSION']}")
        except Exception as e:
            logging.warning(f"[VERSION] Failed to fetch latest Kittyhack version at startup: {e}")
    else:
        logging.warning("Timeout for network connectivity reached. Proceeding without network connection.")
    if is_remote_mode() and remote_setup_required:
        logging.warning("[REMOTE_MODE] Remote configuration missing; backend startup deferred.")
    else:
        start_backend_if_needed()

    # Log the relevant installed deb packages
    SystemInfo.log_relevant_deb_packages()

    # Set the WLAN TX Power level and disable power-save (no-op in remote-mode).
    if is_remote_mode():
        logging.info("Remote-mode detected: skipping WLAN txpower and power-save configuration.")
    else:
        WlanManager.apply_wlan_runtime_settings()

    try:
        sync_pir_second_factor_suggestion()
    except Exception as e:
        logging.warning(f"[USR_NOTIFICATIONS] Failed to sync PIR-second-factor suggestion: {e}")

    logging.info("Starting frontend...")

    CONFIG["LABELSTUDIO_VERSION"] = LabelStudioInstall.get_labelstudio_installed_version()
    free_disk_space = SystemInfo.get_free_disk_space()

    logging.info("Checking ids with images in the database...")
    ids_with_original_blob = EventsRepo.get_ids_with_original_blob(CONFIG['KITTYHACK_DATABASE_PATH'])
    logging.info(f"Found {len(ids_with_original_blob)} images in the database with original_image blob.")
    if len(ids_with_original_blob) == 0:
        CONFIG['EVENT_IMAGES_FS_MIGRATED'] = True

    if is_remote_mode() and remote_setup_required:
        logging.warning("[REMOTE_MODE] Remote configuration missing; background tasks deferred.")
    else:
        start_background_task_if_needed()
