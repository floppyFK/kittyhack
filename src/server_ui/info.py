"""Info tab, backups, changelogs, update flow."""

import os
import re
import pandas as pd
from datetime import datetime
import time as tm
from src.clock import monotonic_time
from shiny import render, ui, reactive
import logging
from faicons import icon_svg
import threading
import subprocess
from io import BytesIO
import zipfile
import shutil
from src.baseconfig import (
    CONFIG,
    ensure_settings_persisted,
    import_legacy_config_ini,
    redact_sensitive_settings_in_database,
    set_language,
    settings_count,
)
from src.helper import (
    Result,
    SystemInfo,
    Versioning,
    heavy_update_repo_files,
    sigterm_monitor,
    wait_for_network,
)
from src.system import KittyhackUpdater
from src.database import (
    DatabaseCore,
    DbMigrations,
)
from src.paths import kittyhack_root
from src.mode import is_remote_mode
from src.shiny_wrappers import uix
import src.startup as startup
from src.server_ui.state import set_update_progress, get_update_progress
from src.server_ui.context import SessionContext
from src.server_ui.helpers import changelog_expandable_item, centered_form_row

_ = set_language(CONFIG["LANGUAGE"])

if is_remote_mode():
    from src.remote.hardware import Magnets, Pir  # type: ignore
else:
    from src.magnets_rfid import Magnets
    from src.pir import Pir


def register_info(input, output, session, ctx: SessionContext):
    """Register Info tab handlers (logs, backups, changelogs, update)."""

    def _signal_log_download_finished() -> None:
        """Tell the browser the archive is ready so it can close the overlay."""
        try:
            session._send_message_sync({"custom": {"log-download-finished": {}}})
        except Exception:
            logging.exception("[DOWNLOAD_LOGS] Failed to close the preparing dialog")

    @render.download(filename="kittyhack_logs.zip")
    def download_logfile():
        try:
            # Create a temporary directory for the logs
            temp_dir = os.path.join("/tmp", f"kittyhack_logs_{int(tm.time())}")
            os.makedirs(temp_dir, exist_ok=True)

            # Path to the zip file we'll create
            zip_file_path = os.path.join("/tmp", f"kittyhack_logs.zip")

            # If the zip file already exists, remove it
            if os.path.exists(zip_file_path):
                os.remove(zip_file_path)

                # Export journal to a file. Do NOT filter by unit — include all system logs.
            journal_file_path = os.path.join(temp_dir, "journalctl.log")
            try:
                result = subprocess.run(
                    [
                        "/usr/bin/journalctl",
                        "-n",
                        "50000",
                        "--no-pager",
                        "--quiet",
                        "--output=short-iso-precise",
                    ],
                    capture_output=True,
                    text=True,
                )
                # If returncode != 0 capture whatever output we got and stderr for diagnostics
                if result.returncode == 0:
                    journal_text = result.stdout
                else:
                    logging.error(
                        f"journalctl exited with code {result.returncode}: {result.stderr.strip()}"
                    )
                    journal_text = (
                        (result.stdout or "")
                        + "\n\n--- journalctl stderr ---\n\n"
                        + (result.stderr or "")
                    )
            except FileNotFoundError:
                logging.error("journalctl not found on system")
                journal_text = "ERROR: journalctl not found on system. No systemd journal available."
            except Exception as e:
                logging.error(f"Failed to export journal: {e}")
                journal_text = f"ERROR: Failed to run journalctl: {e}"

                # Ensure we always write a file (even if empty or containing the error message)
            try:
                with open(journal_file_path, "w", encoding="utf-8") as jf:
                    jf.write(journal_text or "No journal output captured.")
            except Exception as e:
                logging.error(f"Failed to write journal file {journal_file_path}: {e}")
                # continue — zip will be created without the journal

                # If this instance runs in remote-mode, also try to fetch the target-mode journal.
            target_journal_file_path = os.path.join(temp_dir, "journalctl_target.log")
            if is_remote_mode():
                try:
                    from src.remote.control_client import RemoteControlClient

                    client = RemoteControlClient.instance()
                    client.ensure_started()
                    response = client.request_target_journal(lines=10000, timeout=20.0)

                    if response is None:
                        target_journal_text = "ERROR: Failed to fetch target journal (remote control not connected or timeout)."
                    else:
                        target_journal_text = str(response.get("text") or "")
                        reason = str(response.get("reason") or "").strip()
                        truncated = bool(response.get("truncated", False))

                        if (
                            not bool(response.get("ok", False))
                            and not target_journal_text
                        ):
                            target_journal_text = (
                                f"ERROR: Target journal request failed. {reason}"
                            )

                        if truncated:
                            target_journal_text += "\n\n--- NOTE ---\nTarget journal output was truncated to the newest 8 MiB."
                        if reason and bool(response.get("ok", False)):
                            target_journal_text += f"\n\n--- NOTE ---\n{reason}"

                    with open(target_journal_file_path, "w", encoding="utf-8") as tjf:
                        tjf.write(
                            target_journal_text or "No target journal output captured."
                        )
                except Exception as e:
                    logging.error(f"Failed to fetch/write target journal: {e}")
                    try:
                        with open(
                            target_journal_file_path, "w", encoding="utf-8"
                        ) as tjf:
                            tjf.write(f"ERROR: Failed to fetch target journal: {e}")
                    except Exception:
                        pass

                        # Create a zip file with compression: journal, redacted database, setup logs
            with zipfile.ZipFile(
                zip_file_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as z:
                # Add the journalctl export
                if os.path.exists(journal_file_path):
                    z.write(journal_file_path, arcname="journalctl.log")

                    # Add target-mode journal when available (remote-mode only).
                if os.path.exists(target_journal_file_path):
                    z.write(target_journal_file_path, arcname="journalctl_target.log")

                # Include a consistent kittyhack.db snapshot (same approach as download_kittyhack_db).
                # Sensitive settings are masked in this copy only. The dedicated database
                # download stays intact so a restore still has the real credentials.
                db_arcname = "kittyhack_database_sanitized.db"
                if (
                    startup.ids_with_original_blob
                    and len(startup.ids_with_original_blob) > 0
                ):
                    note_path = os.path.join(
                        temp_dir, "kittyhack_database_unavailable.txt"
                    )
                    with open(note_path, "w", encoding="utf-8") as note_file:
                        note_file.write(
                            "Kittyhack database snapshot is currently unavailable.\n"
                            f"Legacy image migration in progress: "
                            f"{len(startup.ids_with_original_blob)} pictures remaining.\n"
                            "Please try again later, or use the dedicated database download.\n"
                        )
                    z.write(note_path, arcname="kittyhack_database_unavailable.txt")
                else:
                    db_snapshot_path = os.path.join(temp_dir, db_arcname)
                    result = DatabaseCore.backup_database_sqlite(
                        CONFIG["KITTYHACK_DATABASE_PATH"], db_snapshot_path
                    )
                    if result.success and os.path.exists(db_snapshot_path):
                        try:
                            redact_sensitive_settings_in_database(db_snapshot_path)
                        except Exception as redact_error:
                            logging.error(
                                f"[DOWNLOAD_LOGS] Failed to redact settings snapshot: {redact_error}"
                            )
                            note_path = os.path.join(
                                temp_dir, "kittyhack_database_unavailable.txt"
                            )
                            with open(note_path, "w", encoding="utf-8") as note_file:
                                note_file.write(
                                    "Kittyhack database snapshot was omitted because "
                                    "sensitive settings could not be redacted.\n"
                                    f"{redact_error}\n"
                                )
                            z.write(
                                note_path, arcname="kittyhack_database_unavailable.txt"
                            )
                        else:
                            z.write(db_snapshot_path, arcname=db_arcname)
                    else:
                        logging.error(
                            f"[DOWNLOAD_LOGS] Database snapshot failed: {result.message}"
                        )
                        note_path = os.path.join(
                            temp_dir, "kittyhack_database_unavailable.txt"
                        )
                        with open(note_path, "w", encoding="utf-8") as note_file:
                            note_file.write(
                                f"Failed to create database snapshot: {result.message}\n"
                            )
                        z.write(
                            note_path, arcname="kittyhack_database_unavailable.txt"
                        )

                system_log_dir = "/var/log"
                if os.path.exists(system_log_dir):
                    # First, find all setup directories with timestamps
                    setup_dirs = []
                    for root, dirs, x in os.walk(system_log_dir):
                        for dir_name in dirs:
                            if dir_name.startswith("kittyhack-setup-"):
                                full_path = os.path.join(root, dir_name)
                                try:
                                    # Extract timestamp from directory name (format: kittyhack-setup-YYYYMMDD-HHMMSS)
                                    timestamp_str = dir_name.replace(
                                        "kittyhack-setup-", ""
                                    )
                                    timestamp = datetime.strptime(
                                        timestamp_str, "%Y%m%d-%H%M%S"
                                    )
                                    setup_dirs.append((full_path, timestamp))
                                except ValueError:
                                    # If timestamp parsing fails, still include with minimum date
                                    setup_dirs.append((full_path, datetime.min))

                                    # Process setup directories (latest first) - include up to 3 most recent directories
                    if setup_dirs:
                        # Sort by timestamp (newest first)
                        setup_dirs.sort(key=lambda x: x[1], reverse=True)
                        # Include up to the latest 3 setup directories
                        for i, (setup_dir, timestamp) in enumerate(setup_dirs[:3]):
                            # Get a short name for the directory based on its timestamp
                            if timestamp != datetime.min:
                                dir_short_name = timestamp.strftime("%Y%m%d-%H%M%S")
                            else:
                                dir_short_name = f"unknown-{i + 1}"

                            for root, x, files in os.walk(setup_dir):
                                for file in files:
                                    file_path = os.path.join(root, file)
                                    rel_path = os.path.relpath(
                                        file_path, system_log_dir
                                    )
                                    z.write(
                                        file_path,
                                        arcname=f"system_logs/{dir_short_name}/{rel_path}",
                                    )

                                    # Clean up the temp directory files
            for file in os.listdir(temp_dir):
                try:
                    os.remove(os.path.join(temp_dir, file))
                except:
                    pass
            try:
                os.rmdir(temp_dir)
            except:
                pass

            return zip_file_path
        except Exception as e:
            logging.error(f"Failed to create logs zip file: {e}")
            ui.notification_show(
                _(
                    "The log archive could not be created. Check the logs, then try again: {}"
                ).format(e),
                duration=10,
                type="error",
            )
            return None
        finally:
            _signal_log_download_finished()

    @render.download(
        filename=lambda: (
            "kittyhack_database_unavailable.txt"
            if (
                startup.ids_with_original_blob
                and len(startup.ids_with_original_blob) > 0
            )
            else "kittyhack_database.db"
        )
    )
    def download_kittyhack_db():
        try:
            from io import BytesIO

            # Block snapshot download while legacy image migration is in progress
            if (
                startup.ids_with_original_blob
                and len(startup.ids_with_original_blob) > 0
            ):
                # Inform user and return a small text file to avoid server-side exceptions
                ui.notification_show(
                    _(
                        "The database snapshot is unavailable while pictures are being migrated. {} pictures remain. Try again when the migration is complete."
                    ).format(len(startup.ids_with_original_blob)),
                    duration=12,
                    type="warning",
                )
                msg = (
                    "Kittyhack database snapshot is currently unavailable.\n"
                    f"Legacy image migration in progress: {len(startup.ids_with_original_blob)} pictures remaining.\n"
                    "Please try again later."
                )
                return BytesIO(msg.encode("utf-8"))

            dest_path = os.path.join("/tmp", "kittyhack_database.db")
            result = DatabaseCore.backup_database_sqlite(
                CONFIG["KITTYHACK_DATABASE_PATH"], dest_path
            )
            if not result.success:
                logging.error(f"[DOWNLOAD_DB] Backup snapshot failed: {result.message}")
                ui.notification_show(
                    _(
                        "A consistent database snapshot could not be created. Check the logs, then try again: {}"
                    ).format(
                        result.message
                    ),
                    duration=12,
                    type="error",
                )
                # Ensure we don't return None; provide a small error text payload
                err_msg = f"Failed to create database snapshot: {result.message}\nCheck server logs for details."
                try:
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                except Exception:
                    pass
                return BytesIO(err_msg.encode("utf-8"))

            return dest_path
        except Exception as e:
            logging.error(f"[DOWNLOAD_DB] Unexpected error: {e}")
            ui.notification_show(
                _(
                    "The database download could not be prepared. Check the logs, then try again: {}"
                ).format(e),
                duration=12,
                type="error",
            )
            # Return a minimal error payload to avoid NoneType iteration errors
            from io import BytesIO

            return BytesIO(
                f"Unexpected error while preparing download: {e}".encode("utf-8")
            )

    @render.download(filename="kittyflap.db")
    def download_kittyflap_db():
        if os.path.exists(CONFIG["DATABASE_PATH"]):
            return CONFIG["DATABASE_PATH"]
        else:
            ui.notification_show(
                _("The original Kittyflap database file was not found."),
                duration=10,
                type="error",
            )
            return None

    @reactive.Effect
    @reactive.event(input.btn_retry_latest_version)
    def on_retry_latest_version():
        try:
            # Ensure network is up, then fetch latest version
            if wait_for_network(timeout=5):
                latest = Versioning.read_latest_kittyhack_version(timeout=5)
                CONFIG["LATEST_VERSION"] = latest
                if latest == "unknown":
                    ui.notification_show(
                        _(
                            "The latest version could not be checked. Check the internet connection, then try again."
                        ),
                        duration=8,
                        type="warning",
                    )
                elif Versioning.is_same_kittyhack_version(startup.git_version, latest):
                    ui.notification_show(
                        _("You are already using the latest version of Kittyhack."),
                        duration=8,
                        type="message",
                    )
                else:
                    ui.notification_show(
                        _("Kittyhack {} is available.").format(latest),
                        duration=8,
                        type="message",
                    )
            else:
                ui.notification_show(
                    _(
                        "No internet connection is available. Check the network, then try again."
                    ),
                    duration=8,
                    type="warning",
                )
        except Exception as e:
            logging.warning(f"[VERSION] Retry failed: {e}")
            ui.notification_show(
                _(
                    "The version check failed again. Check the internet connection and logs, then try later: {}"
                ).format(e),
                duration=10,
                type="error",
            )
        finally:
            # Trigger re-render of INFO section
            ctx.reload_trigger_info.set(ctx.reload_trigger_info.get() + 1)

    @output
    @render.ui
    @reactive.event(ctx.reload_trigger_info, ignore_none=True)
    def ui_info():
        target_git_version = "unknown"
        remote_target_connected = False
        if is_remote_mode():
            try:
                from src.remote.control_client import RemoteControlClient

                client = RemoteControlClient.instance()
                client.ensure_started()
                remote_target_connected = bool(client.wait_until_ready(timeout=0))
                info = (
                    client.request_target_version(timeout=0.6)
                    if remote_target_connected
                    else None
                )
                if not info:
                    info = client.get_target_version_info()
                if info and str(info.get("git_version") or "").strip():
                    target_git_version = str(info.get("git_version") or "unknown")
            except Exception:
                target_git_version = "unknown"

        versions_mismatch = bool(
            is_remote_mode()
            and remote_target_connected
            and target_git_version not in ("", "unknown")
            and startup.git_version not in ("", "unknown")
            and str(target_git_version) != str(startup.git_version)
        )

        if versions_mismatch and not ctx.version_mismatch_warning_shown:
            ui.notification_show(
                _(
                    "Kittyhack versions differ between this device ({}) and the Kittyflap ({}). Update one or both so they match."
                ).format(startup.git_version, target_git_version),
                duration=20,
                type="warning",
            )
            ctx.version_mismatch_warning_shown = True
        elif (not versions_mismatch) and ctx.version_mismatch_warning_shown:
            ctx.version_mismatch_warning_shown = False

        # 2 GB hard gate only when REQUIRED_PYTHON or this host's requirements file will change.
        from src.helper import MIN_HEAVY_UPDATE_FREE_DISK_MB, free_disk_mb_for_heavy_update

        startup.free_disk_space = SystemInfo.get_free_disk_space()
        latest_version = CONFIG["LATEST_VERSION"]
        runtime_changes = {path: False for path in heavy_update_repo_files()}
        if latest_version and latest_version != "unknown":
            try:
                runtime_changes = Versioning.get_runtime_file_changes(latest_version)
            except Exception as e:
                logging.warning(f"[UPDATE] Heavy-update file compare failed: {e}")
        needs_heavy_disk = any(runtime_changes.values())
        is_venv_update = bool(runtime_changes.get("setup/REQUIRED_PYTHON"))
        # .venv.old is deleted before .venv.new is built, so count it only then.
        free_for_update = free_disk_mb_for_heavy_update(
            startup.free_disk_space, runtime_changes
        )
        update_disabled = bool(
            needs_heavy_disk and free_for_update < MIN_HEAVY_UPDATE_FREE_DISK_MB
        )
        update_disk_warning = None
        if update_disabled:
            update_disk_warning = ui.div(
                ui.markdown(
                    f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} "
                    + _(
                        "The update needs at least 2 GB of free disk space, but only {:.1f} MB is available. "
                        "Free some space, for example by reducing the picture limit, then reload this page."
                    ).format(free_for_update)
                ),
                style_="overflow-wrap: anywhere; word-break: break-word; max-width: 100%;",
            )
        venv_update_notice = None
        if is_venv_update and not update_disabled:
            venv_update_notice = ui.div(
                ui.markdown(
                    f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} "
                    + _(
                        "This update includes a Python runtime change and can take **up to 20 minutes**. "
                        "After the reboot, finishing the update can take **another 10–20 minutes**. "
                        "Do **not** power-cycle or switch off the Kittyflap during that time."
                    )
                ),
                style_="overflow-wrap: anywhere; word-break: break-word; max-width: 100%;",
            )

        # Check if the current version is different from the latest version
        if latest_version == "unknown":
            ui_update_kittyhack = ui.div(
                ui.markdown(
                    _(
                        "The latest version could not be loaded from GitHub. Check the internet connection, then try again."
                    )
                ),
            )

            if versions_mismatch and startup.git_repo_available:
                ui_update_kittyhack = (
                    ui_update_kittyhack,
                    ui.hr(),
                    ui.div(
                        ui.markdown(
                            _("Kittyhack versions do not match.")
                            + " "
                            + _(
                                "Update either the Kittyflap or this device to make the versions match."
                            )
                        ),
                        ui.div(
                            ui.input_task_button(
                                "update_target_kittyhack",
                                _("Update Kittyflap only"),
                                icon=icon_svg("download"),
                                class_="btn-default",
                                disabled=update_disabled,
                            ),
                            ui.br(),
                            ui.br(),
                            ui.input_task_button(
                                "update_remote_kittyhack",
                                _("Update this device only"),
                                icon=icon_svg("download"),
                                class_="btn-default",
                                disabled=update_disabled,
                            ),
                            style_="text-align: center;",
                        ),
                        update_disk_warning if update_disk_warning else ui.div(),
                        venv_update_notice if venv_update_notice else ui.div(),
                    ),
                )
        elif not Versioning.is_same_kittyhack_version(
            startup.git_version, latest_version
        ):
            release_notes_block = None
            try:
                # Fetch the release notes of the latest version
                release_notes = Versioning.fetch_github_release_notes(latest_version)
                release_notes = Versioning.filter_release_notes_for_language(
                    release_notes, CONFIG.get("LANGUAGE", "en")
                )
                release_notes_block = ui.div(
                    ui.markdown(
                        "**" + _("Release notes for") + " " + latest_version + ":**"
                    ),
                    ui.div(ui.markdown(release_notes), class_="release_notes"),
                    ui.br(),
                )
            except Exception as e:
                logging.warning(f"[VERSION] Failed to fetch release notes: {e}")
                release_notes_block = ui.div(
                    ui.markdown(
                        _(
                            "Release notes could not be loaded. Check the internet connection, then try again: {}"
                        ).format(e)
                    ),
                    ui.br(),
                )

            ui_update_kittyhack = (
                release_notes_block if release_notes_block else ui.div()
            )

            if startup.git_repo_available:
                ui_update_kittyhack = (
                    ui_update_kittyhack,
                    ui.div(
                        ui.markdown(
                            _("Automatic update to **{}**:").format(latest_version)
                        ),
                        ui.input_task_button(
                            "update_kittyhack",
                            _("Update Kittyhack"),
                            icon=icon_svg("download"),
                            class_="btn-primary",
                            disabled=update_disabled,
                        ),
                        ui.br(),
                        update_disk_warning if update_disk_warning else ui.div(),
                        venv_update_notice if venv_update_notice else ui.div(),
                        ui.help_text(
                            _(
                                "Keep the WLAN connection stable while the update runs."
                            )
                        ),
                        ui.br(),
                        ui.help_text(
                            _("The Kittyflap will restart when the update is complete.")
                        ),
                        ui.markdown(
                            _(
                                "Read the [changelog](https://github.com/floppyFK/kittyhack/releases) to see what changed."
                            )
                        ),
                    ),
                )

                if versions_mismatch:
                    ui_update_kittyhack = (
                        ui_update_kittyhack,
                        ui.hr(),
                        ui.div(
                            ui.markdown(
                                _("Kittyhack versions do not match.")
                                + " "
                                + _(
                                    "Choose one device to update if you want to align the versions first."
                                )
                            ),
                            ui.div(
                                ui.input_task_button(
                                    "update_target_kittyhack",
                                    _("Update Kittyflap only"),
                                    icon=icon_svg("download"),
                                    class_="btn-default",
                                    disabled=update_disabled,
                                ),
                                ui.br(),
                                ui.br(),
                                ui.input_task_button(
                                    "update_remote_kittyhack",
                                    _("Update this device only"),
                                    icon=icon_svg("download"),
                                    class_="btn-default",
                                    disabled=update_disabled,
                                ),
                                style_="text-align: center;",
                            ),
                        ),
                    )
            else:
                ui_update_kittyhack = (
                    ui_update_kittyhack,
                    ui.div(
                        ui.markdown(
                            _(
                                "This Kittyhack installation is not a Git clone, so automatic updates are unavailable. Reinstall it as a Git clone to enable them."
                            )
                        )
                    ),
                )

            if startup.git_repo_available:
                try:
                    # Check for local changes in the git repository and warn the user
                    result = subprocess.run(
                        ["/bin/git", "status", "--porcelain"],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    if result.stdout.strip():
                        result = subprocess.run(
                            ["/bin/git", "status"],
                            capture_output=True,
                            text=True,
                            check=True,
                        )
                        ui_update_kittyhack = (
                            ui_update_kittyhack,
                            ui.div(
                                ui.hr(),
                                ui.markdown(
                                    f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} "
                                    + _(
                                        "Local changes were found in the Git repository at `{}`."
                                    ).format(kittyhack_root())
                                    + "\n\n"
                                    + _(
                                        "Updating will discard these changes. The database and configuration will not be affected."
                                    )
                                    + "\n\n"
                                    + _(
                                        "Commit or stash the changes before updating if you want to keep them."
                                    )
                                ),
                                ui.h6(_("Local changes:")),
                                ui.div(
                                    result.stdout,
                                    class_="release_notes",
                                    style_="font-family: monospace; white-space: pre-wrap;",
                                ),
                            ),
                        )
                except Exception as e:
                    ui_update_kittyhack = (
                        ui_update_kittyhack,
                        ui.div(
                            ui.hr(),
                            ui.markdown(
                                _(
                                    "Local Git changes could not be checked. Check the repository and logs before updating: {}"
                                ).format(e)
                            ),
                        ),
                    )
            else:
                # No local git repo: update button is hidden
                pass

        else:
            ui_update_kittyhack = ui.markdown(
                _("You are already using the latest version of Kittyhack.")
            )
            if versions_mismatch and startup.git_repo_available:
                ui_update_kittyhack = (
                    ui_update_kittyhack,
                    ui.hr(),
                    ui.div(
                        ui.markdown(_("Kittyhack versions do not match.")),
                        ui.div(
                            ui.input_task_button(
                                "update_target_kittyhack",
                                _("Update Kittyflap only"),
                                icon=icon_svg("download"),
                                class_="btn-default",
                                disabled=update_disabled,
                            ),
                            ui.br(),
                            ui.br(),
                            ui.input_task_button(
                                "update_remote_kittyhack",
                                _("Update this device only"),
                                icon=icon_svg("download"),
                                class_="btn-default",
                                disabled=update_disabled,
                            ),
                            style_="text-align: center;",
                        ),
                        update_disk_warning if update_disk_warning else ui.div(),
                        venv_update_notice if venv_update_notice else ui.div(),
                    ),
                )

                # Force-update block: always offer to re-run the update, regardless of
                # version-equality. Useful when tracking a branch (commits may change
                # without the version string moving) or when testing a fork where the
                # latest release tag equals the currently installed one.
        if startup.git_repo_available:
            update_repo_mode = (
                str(CONFIG.get("UPDATE_REPOSITORY_MODE") or "standard").strip().lower()
            )
            if update_repo_mode == "custom":
                force_update_help = _(
                    "The update source is your custom repository. Use this button to pull the latest commit from the selected branch or reinstall the selected tag."
                )
            elif update_repo_mode == "beta":
                force_update_help = _(
                    "The current update source is the official beta channel. "
                    "Use this button to reinstall the latest beta tag (`vX.Y.Z_beta_N`), "
                    "or the latest stable release when no newer beta is available."
                )
            else:
                force_update_help = _(
                    "Reinstall the selected version from the configured update source."
                )
            # Show disk/venv notices next to Force update when the primary Update
            # button is not shown (already-latest / unknown-version paths).
            show_notices_at_force = Versioning.is_same_kittyhack_version(
                startup.git_version, latest_version
            ) or latest_version == "unknown"
            ui_update_kittyhack = (
                ui_update_kittyhack,
                ui.hr(),
                ui.div(
                    ui.h6(_("Reinstall update")),
                    ui.help_text(force_update_help),
                    ui.br(),
                    ui.div(
                        ui.input_task_button(
                            "btn_force_update_kittyhack",
                            _("Reinstall now"),
                            icon=icon_svg("rotate"),
                            class_="btn-default",
                            disabled=update_disabled,
                        ),
                        style_="text-align: center;",
                    ),
                    (
                        update_disk_warning
                        if show_notices_at_force and update_disk_warning
                        else ui.div()
                    ),
                    (
                        venv_update_notice
                        if show_notices_at_force and venv_update_notice
                        else ui.div()
                    ),
                ),
            )

            # Check if the original kittyflap database file still exists
        kittyflap_db_file_exists = os.path.exists(CONFIG["DATABASE_PATH"])
        ui_kittyflap_section = None
        if kittyflap_db_file_exists and (
            SystemInfo.get_file_size(CONFIG["DATABASE_PATH"]) > 50
        ):
            ui_kittyflap_db = ui.div(
                ui.markdown(
                    _(
                        "The original Kittyflap database currently uses **{:.1f} MB** of disk space."
                    ).format(SystemInfo.get_file_size(CONFIG["DATABASE_PATH"]))
                    + "\n\n"
                    + _(
                        "It contains pictures that could no longer be uploaded to the original Kittyflap servers."
                    )
                    + "\n\n"
                    + _("Remove these pictures if you want to free disk space.")
                ),
                ui.input_task_button(
                    "clear_kittyflap_db",
                    _("Remove original pictures"),
                    icon=icon_svg("trash"),
                ),
                ui.download_button(
                    "download_kittyflap_db",
                    _("Download Kittyflap database"),
                    icon=icon_svg("download"),
                ),
            )

            # Build the whole section (card) only if the file exists
            ui_kittyflap_section = ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4(
                            _("Original Kittyflap database"),
                            style_="text-align: center;",
                        )
                    ),
                    ui.br(),
                    ui_kittyflap_db,
                    ui.br(),
                    full_screen=False,
                    class_="generic-container",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px",
            )

        return ui.div(
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4(_("Information"), style_="text-align: center;")
                    ),
                    ui.br(),
                    ui.markdown(
                        _(
                            "Kittyhack is open-source software that lets you use the Kittyflap cat flap locally, without an internet connection."
                        )
                        + "\n\n"
                        + _(
                            "It was created after the Kittyflap manufacturer filed for bankruptcy and its app stopped working."
                        )
                    ),
                    ui.hr(),
                    ui.markdown(
                        _("**About this project**")
                        + "\n\n"
                        + _(
                            "This independent project is not connected to the Kittyflap manufacturer. I created it so I could keep using my Kittyflap."
                        )
                        + "\n\n"
                        + _(
                            "Report bugs or share suggestions on GitHub."
                        )
                    ),
                    ui.HTML(
                        "<center><p><a href='https://github.com/floppyFK/kittyhack' target='_blank'>"
                        + str(icon_svg("square-github"))
                        + " "
                        + _("Open GitHub repository")
                        + "</a></p></center>"
                    ),
                    ui.hr(),
                    ui.markdown(
                        _("**License**")
                        + "\n\n"
                        + _("This project is licensed under the MIT License.")
                        + " "
                        + _("Copyright (c) 2025 Florian Kispert.")
                    ),
                    ui.HTML(
                        "<center><p><a href='https://github.com/floppyFK/kittyhack/blob/main/LICENSE' target='_blank'>"
                        + str(icon_svg("file-lines"))
                        + " "
                        + _("MIT License (full text)")
                        + "</a></p></center>"
                    ),
                    ui.HTML(
                        "<center><p style='margin-bottom: 0.3rem;'>"
                        + _(
                            "If Kittyhack is useful to you, you can support the project with a small donation."
                        )
                        + "</p></center>"
                    ),
                    ui.HTML(
                        "<center><p><a href='https://www.paypal.com/donate?hosted_button_id=QY57YUADYRVW2' target='_blank' class='btn btn-primary'>"
                        + str(icon_svg("paypal"))
                        + " "
                        + _("Donate via PayPal")
                        + "</a></p></center>"
                    ),
                    ui.br(),
                    full_screen=False,
                    class_="generic-container",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px",
            ),
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4(_("Version information"), style_="text-align: center;")
                    ),
                    ui.br(),
                    ui.markdown(
                        "**"
                        + _("Current version")
                        + ":** `"
                        + startup.git_version
                        + "`"
                        + "  \n"
                        + (
                            "**"
                            + _("Kittyflap version")
                            + ":** `"
                            + target_git_version
                            + "`"
                            + "  \n"
                            if is_remote_mode()
                            else ""
                        )
                        + "**"
                        + _("Latest version")
                        + ":** `"
                        + latest_version
                        + "`"
                    ),
                    (
                        ui.help_text(
                            _(
                                "Automatic update checks are turned off. Use this button to look for a new version."
                            )
                        )
                        if not CONFIG["PERIODIC_VERSION_CHECK"]
                        else ui.HTML("")
                    ),
                    ui.div(
                        ui.input_task_button(
                            "btn_retry_latest_version",
                            _("Check for updates"),
                            icon=icon_svg("rotate"),
                        ),
                        style_="text-align: center;",
                    ),
                    (
                        ui.div(
                            ui.markdown(
                                f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} "
                                + _(
                                    "Kittyhack versions differ between this device and the Kittyflap. Some features may not work correctly until both versions match."
                                )
                            ),
                            class_="generic-container warning-container",
                        )
                        if versions_mismatch
                        else ui.HTML("")
                    ),
                    ui_update_kittyhack,
                    ui.br(),
                    ui.h5("Changelogs"),
                    ui.div(
                        ui.input_action_button(
                            "btn_changelogs",
                            _("Show all changelogs"),
                            icon=icon_svg("info"),
                        )
                    ),
                    ui.br(),
                    full_screen=False,
                    class_="generic-container",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px",
            ),
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4(_("System information"), style_="text-align: center;")
                    ),
                    ui.br(),
                    ui.output_ui("ui_system_info"),
                    ui.hr(),
                    ui.h5(_("Kittyhack database")),
                    ui.h6(_("Back up or restore the Kittyhack database")),
                    ui.div(
                        ui.download_button(
                            "download_kittyhack_db",
                            _("Download Kittyhack database"),
                            icon=icon_svg("download"),
                        )
                    ),
                    ui.br(),
                    # Group: Restore Kittyhack DB
                    ui.div(
                        ui.div(
                            centered_form_row(
                                uix.input_file(
                                    "upload_kittyhack_db",
                                    _("Choose Kittyhack database backup (.db)"),
                                    accept=[".db"],
                                    multiple=False,
                                    width="90%",
                                ),
                            ),
                            ui.div(
                                ui.input_task_button(
                                    "restore_kittyhack_db",
                                    _("Restore database"),
                                    icon=icon_svg("rotate"),
                                    class_="btn-outline-danger",
                                ),
                                style_="text-align: center; margin-top: 6px;",
                            ),
                            class_="generic-container",
                            style_=(
                                "border: 1px solid var(--bs-border-color, #ddd); border-radius: 6px; padding: 10px; margin-top: 8px;"
                                "background: var(--bs-tertiary-bg, #fafafa);"
                            ),
                        ),
                        ui.div(
                            changelog_expandable_item(
                                "legacy_config_restore",
                                _("Restore configuration from before v3.0"),
                                ui.div(
                                    ui.markdown(
                                        _(
                                            "Kittyhack versions before v3.0 stored their configuration in `config.ini`. "
                                            "Upload that file here after restoring a database backup from before v3.0."
                                        )
                                    ),
                                    centered_form_row(
                                        uix.input_file(
                                            "upload_legacy_config",
                                            _(
                                                "Choose legacy configuration backup (config.ini)"
                                            ),
                                            accept=[".ini"],
                                            multiple=False,
                                            width="90%",
                                        ),
                                    ),
                                    ui.div(
                                        ui.input_task_button(
                                            "restore_legacy_config",
                                            _("Restore legacy configuration"),
                                            icon=icon_svg("rotate"),
                                            class_="btn-outline-danger",
                                        ),
                                        style_="text-align: center; margin-top: 6px;",
                                    ),
                                ),
                            ),
                            class_="changelog-accordion",
                            style_="margin-top: 8px; max-height: none;",
                        ),
                    ),
                    ui.br(),
                    ui.markdown(
                        _(
                            "After you restore the database, the Kittyflap must restart to apply it."
                        )
                    ),
                    full_screen=False,
                    class_="generic-container",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px",
            ),
            # Include Original Kittyflap Database card only if present
            (ui_kittyflap_section if ui_kittyflap_section else ui.HTML("")),
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4(
                            _("Progressive Web App (PWA)"), style_="text-align: center;"
                        )
                    ),
                    ui.br(),
                    ui.markdown(
                        _("You can install Kittyhack as a")
                        + " ["
                        + _("Progressive Web App")
                        + "](https://web.dev/learn/pwa/progressive-web-apps) "
                        + _("on your smartphone or computer for easier access.")
                        + "  \n"
                        + _(
                            "You can add the PWA to your home screen and open it like an app."
                        )
                    ),
                    ui.div(
                        # HTTPS Warning message
                        ui.div(
                            ui.markdown(
                                f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} "
                                + _("**HTTPS required**")
                                + "\n\n"
                                + _(
                                    "Installing the PWA requires a secure HTTPS connection. Kittyhack is currently open over HTTP."
                                )
                                + "  \n\n"
                                + _("Set up a reverse proxy with HTTPS to continue.")
                                + " "
                                + _(
                                    "For help setting up a reverse proxy on your home network, see"
                                )
                                + " ["
                                + _("this guide")
                                + "](https://schroederdennis.de/allgemein/nginx-proxy-manager-nginx-reverse-proxy-vorgestellt/)."
                            ),
                            id="pwa_https_warning",
                            style_="display: none; color: #e74a3b; padding: 10px; border: 1px solid #e74a3b; border-radius: 5px; margin: 10px 0;",
                        ),
                        # Already installed message
                        ui.div(
                            ui.markdown(
                                f"{icon_svg('circle-check', margin_left='-0.1em')} "
                                + _("**App is already installed on this device.**")
                            ),
                            id="pwa_already_installed",
                            style_="display: none; color: #1cc88a; padding: 10px; text-align: center;",
                        ),
                        # Success message
                        ui.div(
                            ui.markdown(
                                f"{icon_svg('circle-check', margin_left='-0.1em')} "
                                + _("**App installed.**")
                            ),
                            id="pwa_installed_success",
                            style_="display: none; color: #1cc88a; padding: 10px; text-align: center;",
                        ),
                        # Install button
                        ui.div(
                            ui.input_action_button(
                                id="pwa_install_button",
                                label=_("Install app"),
                                icon=icon_svg("download"),
                                class_="btn-default",
                            ),
                            style_="text-align: center; margin-top: 10px;",
                        ),
                        id="pwa_install_container",
                    ),
                    ui.br(),
                    full_screen=False,
                    class_="generic-container",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px",
            ),
            ui.div(
                ui.card(
                    ui.card_header(ui.h4(_("Log files"), style_="text-align: center;")),
                    ui.br(),
                    ui.div(
                        ui.download_button(
                            "download_logfile",
                            _("Download Kittyhack logs"),
                            icon=icon_svg("download"),
                            data_kh_prepare_title=_("Preparing download"),
                            data_kh_prepare_body=_(
                                "Creating the log archive. This may take a moment."
                            ),
                        )
                    ),
                    ui.br(),
                    full_screen=False,
                    class_="generic-container",
                    style_="padding-left: 1rem !important; padding-right: 1rem !important;",
                ),
                width="400px",
            ),
            ui.br(),
            ui.br(),
            ui.br(),
            ui.br(),
        )

    @render.table
    def ui_system_info():
        reactive.invalidate_later(10.0)

        database_size = SystemInfo.get_database_size()
        startup.free_disk_space = SystemInfo.get_free_disk_space()
        total_disk_space = SystemInfo.get_total_disk_space()
        used_ram_space = SystemInfo.get_used_ram_space()
        total_ram_space = SystemInfo.get_total_ram_space()
        ram_usage_percentage = (used_ram_space / total_ram_space) * 100

        def _wlan_status_icon(color_class: str, title: str):
            try:
                icon = icon_svg("wifi", margin_left="0", margin_right="0")
            except Exception:
                return ui.span("•", class_=f"table-icon {color_class}", title=title)
            return ui.span(icon, class_=f"table-icon {color_class}", title=title)

            # Get WLAN status information

        if is_remote_mode():
            wlan_info = _("Not available in remote mode")
        else:
            try:
                wlan = subprocess.run(
                    ["/sbin/iwconfig", "wlan0"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                if "Link Quality=" in wlan.stdout and "Signal level=" in wlan.stdout:
                    quality = wlan.stdout.split("Link Quality=")[1].split(" ")[0]
                    signal = wlan.stdout.split("Signal level=")[1].split(" ")[0]
                    quality_value = float(quality.split("/")[0]) / float(
                        quality.split("/")[1]
                    )

                    if quality_value >= 0.8:
                        color_class = "text-success"
                        title = _("Strong signal")
                    elif quality_value >= 0.4:
                        color_class = "text-warning"
                        title = _("Medium signal")
                    else:
                        color_class = "text-danger"
                        title = _("Weak signal")

                    wlan_info = ui.span(
                        _wlan_status_icon(color_class, title),
                        " ",
                        _("Quality: {}, Signal: {} dBm").format(quality, signal),
                    )
                else:
                    wlan_info = _("Not connected")
            except Exception:
                wlan_info = _("Unknown")

                # Create a DataFrame with the system information
        df = pd.DataFrame(
            {
                "Property": [
                    _("Kittyhack database size"),
                    _("Free disk space"),
                    _("Used RAM"),
                    _("WLAN status"),
                ],
                "Value": [
                    f"{database_size:.1f} MB",
                    f"{startup.free_disk_space:.1f} MB / {total_disk_space:.1f} MB",
                    f"{used_ram_space:.1f} MB / {total_ram_space:.1f} MB ({ram_usage_percentage:.1f}%)",
                    wlan_info,
                ],
            }
        )
        return df.style.set_table_attributes(
            'class="dataframe shiny-table table w-auto"'
        ).hide(axis="index")

    @reactive.Effect
    @reactive.event(input.btn_changelogs)
    def show_changelogs():
        update_mode = (
            str(CONFIG.get("UPDATE_REPOSITORY_MODE") or "standard").strip().lower()
        )
        entries = Versioning.list_changelogs(
            after_version="v1.0.0",
            language=CONFIG["LANGUAGE"],
            include_beta=(update_mode == "beta"),
        )
        if not entries:
            body = ui.markdown(_("No changelogs are available."))
        else:
            rows = []
            for idx, entry in enumerate(entries):
                title = entry["version"]
                if entry.get("is_beta"):
                    title = f"{title} ({_('Beta')})"
                # Drop a leading "# version" heading from the body — the row title
                # already shows it — to avoid duplicating the version line.
                body_md = entry.get("body") or ""
                body_lines = body_md.splitlines()
                if body_lines and re.match(
                    r"^\s*#+\s*v?\d", body_lines[0], flags=re.IGNORECASE
                ):
                    body_md = "\n".join(body_lines[1:]).lstrip("\n")
                rows.append(
                    changelog_expandable_item(
                        f"changelog_item_{idx}",
                        title,
                        ui.markdown(body_md or _("No release notes are available.")),
                    )
                )
            body = ui.div(*rows, class_="changelog-accordion")

        ui.modal_show(
            ui.modal(
                body,
                title=_("Changelogs"),
                easy_close=True,
                size="xl",
                footer=ui.div(
                    ui.input_action_button("btn_modal_cancel", _("Close")),
                ),
            )
        )

    @reactive.Effect
    @reactive.event(input.clear_kittyflap_db)
    def clear_original_kittyflap_db():
        with ui.Progress(min=1, max=2) as p:
            p.set(
                1,
                message=_("Deleting pictures…"),
                detail=_("This may take a while."),
            )
            if os.path.exists(CONFIG["DATABASE_PATH"]):
                try:
                    DbMigrations.clear_original_kittyflap_database(
                        CONFIG["DATABASE_PATH"]
                    )
                    ui.notification_show(
                        _(
                            "Pictures removed from the original Kittyflap database."
                        ),
                        duration=5,
                        type="message",
                    )
                    p.set(2)
                except Exception as e:
                    ui.notification_show(
                        _(
                            "The pictures could not be removed from the original Kittyflap database. Check the logs, then try again: {}"
                        ).format(e),
                        duration=10,
                        type="error",
                    )
            else:
                ui.notification_show(
                    _("The original Kittyflap database file was already removed."),
                    duration=5,
                    type="message",
                )
        ctx.reload_trigger_info.set(ctx.reload_trigger_info.get() + 1)

    def _selected_db_upload():
        """Return (filename, datapath) for the Info-tab database upload, if any."""
        try:
            files = input.upload_kittyhack_db()
        except Exception as e:
            logging.warning(f"[RESTORE_DB] Could not read the uploaded database: {e}")
            return None
        if not files:
            return None
        uploaded = files[0]
        name = str(uploaded.get("name") or "")
        src_path = str(uploaded.get("datapath") or "")
        return name, src_path

    def _selected_legacy_config_upload():
        """Return (filename, datapath) for the legacy config upload, if any."""
        try:
            files = input.upload_legacy_config()
        except Exception as e:
            logging.warning(
                f"[RESTORE_CFG] Could not read the uploaded configuration: {e}"
            )
            return None
        if not files:
            return None
        uploaded = files[0]
        name = str(uploaded.get("name") or "")
        src_path = str(uploaded.get("datapath") or "")
        return name, src_path

    def _restore_db_error_message(code: str) -> str:
        if code == "invalid_database":
            return _(
                "This file is not a valid Kittyhack database. Choose a database backup."
            )
        if code in {"missing_source", "not_found"}:
            return _("The uploaded file was not found. Choose it again, then retry.")
        return _(
            "The database could not be restored. Check the logs, then try again: {}"
        ).format(code)

    def _show_restore_restart_modal(message: str, *, allow_close: bool = False):
        footer = [
            ui.input_action_button("btn_modal_reboot_ok", _("Restart")),
        ]
        if allow_close:
            footer.append(ui.input_action_button("btn_modal_cancel", _("Close")))
        ui.modal_show(
            ui.modal(
                ui.markdown(message),
                title=_("Restart required"),
                easy_close=False,
                footer=ui.div(*footer),
            )
        )

    @reactive.Effect
    @reactive.event(input.upload_kittyhack_db)
    def on_upload_kittyhack_db():
        selected = _selected_db_upload()
        if not selected:
            ctx.last_uploaded_db_path.set(None)
            ui.notification_show(
                _("Choose a database backup to upload."), duration=8, type="error"
            )
            return
        name, src_path = selected
        logging.info(
            f"[RESTORE_DB] Upload received: name={name!r} path={src_path!r}"
        )
        if not src_path or not os.path.exists(src_path):
            ui.notification_show(
                _("The uploaded file was not found. Choose it again, then retry."),
                duration=8,
                type="error",
            )
            ctx.last_uploaded_db_path.set(None)
            return
        if not os.path.basename(name).lower().endswith(".db"):
            ui.notification_show(
                _("This file is not a .db file. Choose a database backup."),
                duration=10,
                type="error",
            )
            ctx.last_uploaded_db_path.set(None)
            return
        ctx.last_uploaded_db_path.set(src_path)
        ui.notification_show(
            _("Database backup uploaded. Select Restore database to apply it."),
            duration=8,
            type="message",
        )

    @reactive.Effect
    @reactive.event(input.restore_kittyhack_db)
    def on_restore_kittyhack_db():
        logging.info("[RESTORE_DB] Restore requested")
        selected = _selected_db_upload()
        src_path = selected[1] if selected else ctx.last_uploaded_db_path.get()
        name = selected[0] if selected else ""
        if not src_path or not os.path.exists(src_path):
            ui.notification_show(
                _("Choose and upload a database backup before restoring."),
                duration=8,
                type="error",
            )
            return
        if name and not os.path.basename(name).lower().endswith(".db"):
            ui.notification_show(
                _("This file is not a .db file. Choose a database backup."),
                duration=10,
                type="error",
            )
            return
        precheck = DatabaseCore.validate_kittyhack_database(src_path)
        if not precheck.success:
            ui.notification_show(
                _restore_db_error_message(precheck.message),
                duration=12,
                type="error",
            )
            return
        # Stop writers before swapping the file. stop_now does not restart
        # those loops, so a restart is required afterwards even if replace fails.
        try:
            sigterm_monitor.halt_backend()
            tm.sleep(1.0)
            result = DatabaseCore.replace_database_file(
                src_path, CONFIG["KITTYHACK_DATABASE_PATH"]
            )
        except Exception as e:
            logging.error(f"[RESTORE_DB] Failed to restore database: {e}")
            result = Result(False, "replace_failed")
        if not result.success:
            ui.notification_show(
                _restore_db_error_message(result.message),
                duration=12,
                type="error",
            )
            _show_restore_restart_modal(
                _(
                    "The database could not be restored. Restart the Kittyflap, then try again."
                )
            )
            return
        logging.info("Database restore performed --> Restart pending.")
        ui.notification_show(_("Database restored."), duration=6, type="message")
        restored_without_settings = (
            settings_count(CONFIG["KITTYHACK_DATABASE_PATH"]) == 0
        )
        if restored_without_settings:
            if not ensure_settings_persisted():
                logging.error(
                    "[RESTORE_DB] Failed to preserve current settings in restored database."
                )
                ui.notification_show(
                    _(
                        "The database was restored, but the current configuration could not be preserved. "
                        "Upload the matching config.ini before restarting."
                    ),
                    duration=None,
                    type="error",
                )
            _show_restore_restart_modal(
                _(
                    "This older database backup did not contain configuration settings. "
                    "Kittyhack kept the current settings. You can close this dialog and "
                    "upload the matching `config.ini` before restarting."
                ),
                allow_close=True,
            )
        else:
            _show_restore_restart_modal(
                _("Restart the Kittyflap now to apply the restored database.")
            )

    @reactive.Effect
    @reactive.event(input.upload_legacy_config)
    def on_upload_legacy_config():
        selected = _selected_legacy_config_upload()
        if not selected:
            ctx.last_uploaded_cfg_path.set(None)
            ui.notification_show(
                _("Choose a config.ini backup to upload."),
                duration=8,
                type="error",
            )
            return
        name, src_path = selected
        logging.info(
            f"[RESTORE_CFG] Upload received: name={name!r} path={src_path!r}"
        )
        if not src_path or not os.path.exists(src_path):
            ctx.last_uploaded_cfg_path.set(None)
            ui.notification_show(
                _("The uploaded file was not found. Choose it again, then retry."),
                duration=8,
                type="error",
            )
            return
        if not os.path.basename(name).lower().endswith(".ini"):
            ctx.last_uploaded_cfg_path.set(None)
            ui.notification_show(
                _("This file is not an .ini file. Choose a config.ini backup."),
                duration=10,
                type="error",
            )
            return
        ctx.last_uploaded_cfg_path.set(src_path)
        ui.notification_show(
            _(
                "Configuration backup uploaded. Select Restore legacy configuration to apply it."
            ),
            duration=8,
            type="message",
        )

    @reactive.Effect
    @reactive.event(input.restore_legacy_config)
    def on_restore_legacy_config():
        logging.info("[RESTORE_CFG] Legacy configuration restore requested")
        selected = _selected_legacy_config_upload()
        src_path = selected[1] if selected else ctx.last_uploaded_cfg_path.get()
        name = selected[0] if selected else ""
        if not src_path or not os.path.exists(src_path):
            ui.notification_show(
                _("Choose and upload a config.ini backup before restoring."),
                duration=8,
                type="error",
            )
            return
        if name and not os.path.basename(name).lower().endswith(".ini"):
            ui.notification_show(
                _("This file is not an .ini file. Choose a config.ini backup."),
                duration=10,
                type="error",
            )
            return
        ok, message = import_legacy_config_ini(src_path)
        if not ok:
            if message in {"missing_settings_section", "no_supported_settings"}:
                error_message = _(
                    "This file contains no supported Kittyhack settings. Choose a valid config.ini backup."
                )
            elif message == "not_found":
                error_message = _(
                    "The uploaded file was not found. Choose it again, then retry."
                )
            else:
                error_message = _(
                    "The configuration could not be restored. Check the logs, then try again: {}"
                ).format(message)
            ui.notification_show(error_message, duration=12, type="error")
            return
        logging.info("Legacy configuration restore performed --> Restart pending.")
        ui.notification_show(
            _("Legacy configuration restored."), duration=6, type="message"
        )
        _show_restore_restart_modal(
            _("Restart the Kittyflap now to apply the restored configuration.")
        )

    def _start_update_process(
        update_target: bool, update_local: bool, *, force: bool = False
    ):
        latest_version = CONFIG["LATEST_VERSION"]
        current_version = startup.git_version

        if not startup.git_repo_available:
            ui.notification_show(
                _(
                    "Automatic updates require a Git repository. Reinstall Kittyhack with Git clone or the setup script, then try again."
                ),
                duration=12,
                type="error",
            )
            return

        if is_remote_mode() and not (update_target or update_local):
            ui.notification_show(_("Nothing to update."), duration=8, type="warning")
            return

        if (not is_remote_mode()) and update_target:
            ui.notification_show(
                _("Updating only the Kittyflap is available only in remote mode."),
                duration=8,
                type="error",
            )
            return

        # A restored browser click after reboot must not start another update
        # when this device is already on the requested version.
        if (
            not force
            and update_local
            and not update_target
            and Versioning.is_same_kittyhack_version(
                str(current_version or ""), str(latest_version or "")
            )
        ):
            logging.info(
                "[UPDATE] Local version %s already matches %s. Skipping.",
                current_version,
                latest_version,
            )
            return

        if is_remote_mode() and update_target and update_local:
            initial_max_steps = 9
        elif update_local:
            initial_max_steps = 8
        else:
            initial_max_steps = 1

        # Detect Python-runtime (venv) updates before opening the progress modal.
        is_venv_update = False
        if latest_version and latest_version != "unknown":
            try:
                is_venv_update = bool(
                    Versioning.update_changes_required_python(latest_version)
                )
            except Exception as e:
                logging.warning(f"[UPDATE] REQUIRED_PYTHON compare failed: {e}")
                is_venv_update = False

        set_update_progress(
            in_progress=True,
            step=1,
            max_steps=initial_max_steps,
            message=_("Starting update"),
            detail="",
            result=None,
            error_msg="",
            is_venv_update=is_venv_update,
            reboot_required=True,
        )

        # Start the update in a background thread so the UI can update immediately
        def run_update():
            remote_mode_active = bool(is_remote_mode())

            def _set_error_and_stop(message: str, *, reboot_required: bool = True):
                logging.error(f"Kittyhack update failed: {message}")
                set_update_progress(
                    result="error",
                    in_progress=False,
                    error_msg=message,
                    reboot_required=reboot_required,
                )

            local_already_current = Versioning.is_same_kittyhack_version(
                str(current_version or ""), str(latest_version or "")
            )
            target_update_started = False

            if remote_mode_active and update_target:
                try:
                    from src.remote.control_client import RemoteControlClient

                    set_update_progress(
                        in_progress=True,
                        step=1,
                        max_steps=(9 if update_local else 1),
                        message=_("Updating the connected Kittyflap"),
                        detail=_("Preparing the remote update"),
                        result=None,
                        error_msg="",
                    )

                    client = RemoteControlClient.instance()
                    client.ensure_started()
                    if not client.wait_until_ready(timeout=20.0):
                        # Nothing has been checked out yet. A reboot cannot help,
                        # and demanding one restarts the loop after a reconnect.
                        if update_local and local_already_current:
                            message = _(
                                "This device is already up to date. The Kittyflap is still starting or is not connected. Close this message and try again in a minute."
                            )
                        else:
                            message = _(
                                "The Kittyflap is not connected. Check the remote connection, then try again."
                            )
                        _set_error_and_stop(message, reboot_required=False)
                        return

                    client.start_target_update(
                        latest_version=latest_version, current_version=current_version
                    )
                    target_update_started = True
                    target_update_started_at = monotonic_time()
                    reconnect_grace_s = 120.0
                    had_in_progress_state = False
                    had_disconnect_during_target_update = False
                    first_disconnect_at = 0.0

                    while True:
                        if (
                            monotonic_time() - float(target_update_started_at or 0.0)
                        ) > 3600.0:
                            _set_error_and_stop(
                                _(
                                    "The Kittyflap update took too long. Check the connection and logs before trying again."
                                )
                            )
                            return

                        status = client.get_target_update_status()
                        if bool(status.get("in_progress")):
                            had_in_progress_state = True

                        if status.get("ok") is False:
                            reason = str(
                                status.get("reason")
                                or _("Unknown Kittyflap update error")
                            )
                            _set_error_and_stop(
                                _(
                                    "The Kittyflap update failed. Check the logs before trying again: {}."
                                ).format(reason)
                            )
                            return
                        if status.get("ok") is True:
                            set_update_progress(
                                in_progress=True,
                                step=1,
                                max_steps=(9 if update_local else 1),
                                message=_("Kittyflap updated"),
                                detail=(
                                    _("Starting the update on this device")
                                    if update_local
                                    else _("Update finished")
                                ),
                                result=None,
                                error_msg="",
                            )
                            break

                        if not client.wait_until_ready(timeout=0):
                            # After dispatching update_request, an immediate disconnect is expected on some
                            # targets while kittyhack_control updates/restarts services. Treat this as
                            # transient and wait for reconnection within a grace window.
                            elapsed = monotonic_time() - float(
                                target_update_started_at or 0.0
                            )
                            had_disconnect_during_target_update = True
                            if first_disconnect_at <= 0.0:
                                first_disconnect_at = monotonic_time()
                            set_update_progress(
                                in_progress=True,
                                step=1,
                                max_steps=(9 if update_local else 1),
                                message=_("Updating the connected Kittyflap"),
                                detail=_(
                                    "The connection was lost while the Kittyflap restarted. Waiting for it to reconnect."
                                ),
                                result=None,
                                error_msg="",
                            )
                            if elapsed >= reconnect_grace_s:
                                # The install on this device was not changed. Asking
                                # for a reboot here just repeats the same attempt.
                                _set_error_and_stop(
                                    _(
                                        "The Kittyflap did not reconnect after the update. Check its power and network connection."
                                    ),
                                    reboot_required=False,
                                )
                                return
                            tm.sleep(1.0)
                            continue

                            # If we had a disconnect after dispatching update_request and the connection has
                            # now recovered, the target might already be updated even if update_end was lost.
                            # Verify by version and finish instead of waiting forever in "in progress".
                        if had_disconnect_during_target_update and bool(
                            status.get("in_progress")
                        ):
                            since_disconnect = (
                                monotonic_time() - first_disconnect_at
                                if first_disconnect_at > 0.0
                                else 0.0
                            )
                            if since_disconnect >= 3.0:
                                info = (
                                    client.request_target_version(timeout=1.0)
                                    or client.get_target_version_info()
                                )
                                target_git_version = str(
                                    (info or {}).get("git_version") or ""
                                ).strip()
                                if target_git_version and target_git_version not in (
                                    "unknown",
                                ):
                                    if Versioning.is_same_kittyhack_version(
                                        str(target_git_version), str(latest_version)
                                    ):
                                        set_update_progress(
                                            in_progress=True,
                                            step=1,
                                            max_steps=(9 if update_local else 1),
                                            message=_(
                                                "Kittyflap updated"
                                            ),
                                            detail=(
                                                _("Starting the update on this device")
                                                if update_local
                                                else _("Update finished")
                                            ),
                                            result=None,
                                            error_msg="",
                                        )
                                        break

                        if status.get("in_progress"):
                            detail = _("The Kittyflap update is running")
                        elif status.get("requested"):
                            detail = _("Waiting for the Kittyflap update to start")
                        else:
                            detail = _("Waiting for the Kittyflap to respond")

                        set_update_progress(
                            in_progress=True,
                            step=1,
                            max_steps=(9 if update_local else 1),
                            message=_("Updating the connected Kittyflap"),
                            detail=detail,
                            result=None,
                            error_msg="",
                        )
                        tm.sleep(1.0)

                        # If the websocket was dropped during target update, verify the target version
                        # after reconnect instead of treating this as a hard failure.
                    if had_disconnect_during_target_update:
                        try:
                            verify_deadline = monotonic_time() + 20.0
                            target_git_version = ""
                            while monotonic_time() < verify_deadline:
                                if not client.wait_until_ready(timeout=1.0):
                                    continue
                                info = (
                                    client.request_target_version(timeout=1.5)
                                    or client.get_target_version_info()
                                )
                                target_git_version = str(
                                    (info or {}).get("git_version") or ""
                                ).strip()
                                if target_git_version:
                                    break
                            if target_git_version and target_git_version not in (
                                "unknown",
                            ):
                                if not Versioning.is_same_kittyhack_version(
                                    str(target_git_version), str(latest_version)
                                ):
                                    _set_error_and_stop(
                                        _(
                                            "The Kittyflap reconnected, but its version is still {} instead of {}. Check the update logs before trying again."
                                        ).format(
                                            target_git_version,
                                            latest_version,
                                        )
                                    )
                                    return
                                logging.info(
                                    f"[UPDATE] Target update verified after reconnect (version {target_git_version})."
                                )
                            else:
                                logging.warning(
                                    "[UPDATE] Could not verify target version after reconnect. Continuing."
                                )
                        except Exception as e:
                            logging.warning(
                                f"[UPDATE] Post-update target version verification failed: {e}"
                            )
                except Exception as e:
                    _set_error_and_stop(
                        _(
                            "The Kittyflap update could not run. Check the connection and logs, then try again: {}."
                        ).format(e),
                        reboot_required=target_update_started,
                    )
                    return

            if not update_local:
                set_update_progress(result="ok", in_progress=False)
                return

            def progress_callback(step, message, detail):
                mapped_step = int(step)
                mapped_max_steps = 8
                if remote_mode_active and update_target:
                    mapped_step = max(1, min(9, int(step) + 1))
                    mapped_max_steps = 9
                set_update_progress(
                    in_progress=True,
                    step=mapped_step,
                    max_steps=mapped_max_steps,
                    message=message,
                    detail=detail,
                    result=None,
                    error_msg="",
                )

            ok, msg = KittyhackUpdater.update_kittyhack(
                progress_callback=progress_callback,
                latest_version=latest_version,
                current_version=current_version,
            )
            if ok:
                logging.info(
                    f"Kittyhack updated successfully to version {latest_version}."
                )
                set_update_progress(result="ok")
            else:
                logging.error(f"Kittyhack update failed: {msg}")
                set_update_progress(result="error", error_msg=msg)

        threading.Thread(target=run_update, daemon=True).start()

        # Add some delay here to keep the action button active for a moment until the modal is shown
        tm.sleep(1.0)

    @reactive.Effect
    @reactive.event(input.update_kittyhack, ignore_init=True)
    def update_kittyhack_process():
        # In remote-mode: target first, then local device.
        # In target/local mode: local device only.
        _start_update_process(update_target=bool(is_remote_mode()), update_local=True)

    @reactive.Effect
    @reactive.event(input.update_target_kittyhack, ignore_init=True)
    def update_target_kittyhack_process():
        _start_update_process(update_target=True, update_local=False)

    @reactive.Effect
    @reactive.event(input.update_remote_kittyhack, ignore_init=True)
    def update_remote_kittyhack_process():
        _start_update_process(update_target=False, update_local=True)

    @reactive.Effect
    @reactive.event(input.btn_force_update_kittyhack, ignore_init=True)
    def force_update_kittyhack_process():
        # Same flow as the regular update button; the underlying KittyhackUpdater.update_kittyhack()
        # already handles both tag mode (re-checkout) and branch mode (reset to
        # origin/<ref>) correctly when called with the current latest_version.
        # force=True still re-checkouts when this device is already on that version.
        _start_update_process(
            update_target=bool(is_remote_mode()), update_local=True, force=True
        )

    @output
    @render.text
    def update_progress_message():
        reactive.invalidate_later(0.5)
        state = get_update_progress()
        return state["message"]

    @output
    @render.text
    def update_progress_detail():
        reactive.invalidate_later(0.5)
        state = get_update_progress()
        return state["detail"]

    def show_update_progress_modal():
        state = get_update_progress()
        if not state["in_progress"] and state["result"] is None:
            ui.modal_remove()
            return

        timing_note = ui.markdown(
            _("*This step may take several minutes.*")
        )
        if state.get("is_venv_update"):
            timing_note = ui.div(
                ui.markdown(
                    _(
                        "This update includes a Python runtime change and can take **up to 20 minutes**. "
                        "After the reboot, finishing the update can take **another 10–20 minutes**. "
                        "Do **not** power-cycle or switch off the Kittyflap during that time."
                    )
                ),
                style_="overflow-wrap: anywhere; word-break: break-word; max-width: 100%;",
            )

        ui.modal_show(
            ui.modal(
                ui.div(
                    ui.HTML("""
                        <div style="margin-bottom: 1em;">
                            <div style="display: flex; align-items: center; gap: 1em;">
                                <div class="spinner" style="display: inline-block; width: 16px; height: 16px; border: 2px solid #eee; border-top: 2px solid #007bff; border-radius: 50%; animation: spin 1s linear infinite;"></div>
                                <div style="font-weight: bold; display: flex;">
                    """),
                    ui.output_text("update_progress_message"),
                    ui.HTML("""
                                    <div id="in_progress_dots" style="margin-left: 0.2em; color: #888; font-size: 0.95em;"></div>
                                </div>
                            </div>
                            <div style="color: #888; overflow-wrap: anywhere; word-break: break-word; max-width: 100%;">
                    """),
                    ui.output_text("update_progress_detail"),
                    ui.HTML("""
                            </div>
                            <div style="margin-top: 1em;">
                                <div style="background: #eee; border-radius: 4px; height: 24px; width: 100%; position: relative;">
                                    <div id="progress_bar" style="background: #007bff; height: 100%; border-radius: 4px; width: 0%; transition: width 0.3s;"></div>
                                    <div id="progress_percent_text" style="position: absolute; right: 8px; top: 0; height: 100%; display: flex; align-items: center; color: #555; font-size: 0.9em;">
                    """),
                    ui.output_text("update_progress_percent"),
                    ui.HTML("""
                                    </div>
                                </div>
                            </div>
                            <style>
                            @keyframes spin {
                                0% { transform: rotate(0deg);}
                                100% { transform: rotate(360deg);}
                            }
                            </style>
                        </div>
                        <br>
                    """),
                    ui.markdown(
                        _("Keep this page open until the update is complete.")
                    ),
                    timing_note,
                ),
                title=_("Updating Kittyhack"),
                easy_close=False,
                footer=None,
                id="update_progress_modal",
            )
        )

    @output
    @render.text
    def update_progress_percent():
        reactive.invalidate_later(0.5)
        state = get_update_progress()
        max_steps = int(state.get("max_steps") or 0)
        step = int(state.get("step") or 0)
        if max_steps <= 0:
            percent = 0
        else:
            percent = int(round((step / max_steps) * 100))
            percent = max(0, min(100, percent))
        return f"{percent}%"

        # Reactive effect to show/update the modal in all sessions

    @reactive.Effect
    async def update_progress_watcher():
        reactive.invalidate_later(1)
        state = get_update_progress()

        # Track only the modal open/close state
        if not hasattr(update_progress_watcher, "modal_open"):
            update_progress_watcher.modal_open = False
        if not hasattr(update_progress_watcher, "reboot_dialog_shown"):
            update_progress_watcher.reboot_dialog_shown = False
        if not hasattr(update_progress_watcher, "dismiss_dialog_shown"):
            update_progress_watcher.dismiss_dialog_shown = False

            # Show update progress modal only when starting update
        if state["in_progress"] and not update_progress_watcher.modal_open:
            show_update_progress_modal()
            update_progress_watcher.modal_open = True
            update_progress_watcher.reboot_dialog_shown = False
            update_progress_watcher.dismiss_dialog_shown = False

            # Remove modal and show result only when update is finished
        elif (
            update_progress_watcher.modal_open
            and not state["in_progress"]
            and state["result"] is None
        ):
            ui.modal_remove()
            update_progress_watcher.modal_open = False
            update_progress_watcher.reboot_dialog_shown = False
            update_progress_watcher.dismiss_dialog_shown = False

            # Show reboot dialog if update finished and reboot required, but only once
        elif (
            state["result"] == "ok" or state["result"] == "reboot_dialog"
        ) and not update_progress_watcher.reboot_dialog_shown:
            # Always remove any open modal first
            ui.modal_remove()
            set_update_progress(result="reboot_dialog", in_progress=False)
            reboot_body = [
                ui.markdown(
                    _(
                        "The update is ready. Restart the Kittyflap now to apply it."
                    )
                )
            ]
            if state.get("is_venv_update"):
                reboot_body.append(
                    ui.div(
                        ui.markdown(
                            _(
                                "After the reboot, finishing the update can take **another 10–20 minutes**. "
                                "Do **not** power-cycle or switch off the Kittyflap during that time."
                            )
                        ),
                        style_="overflow-wrap: anywhere; word-break: break-word; max-width: 100%; margin-top: 0.75em;",
                    )
                )
            ui.modal_show(
                ui.modal(
                    ui.div(*reboot_body),
                    title=_("Restart required"),
                    easy_close=False,
                    footer=ui.div(
                        ui.input_action_button("btn_modal_reboot_ok", _("Restart")),
                    ),
                )
            )
            update_progress_watcher.modal_open = True
            update_progress_watcher.reboot_dialog_shown = True

            # Connection failures leave the install unchanged. Offer Close,
            # not another reboot, so a restored browser session cannot loop.
        elif (
            state["result"] == "error"
            and not state.get("reboot_required", True)
            and not update_progress_watcher.dismiss_dialog_shown
        ):
            ui.modal_remove()
            error_text = str(state.get("error_msg") or "").strip()
            ui.modal_show(
                ui.modal(
                    ui.div(
                        error_text,
                        style_=(
                            "white-space: pre-wrap; overflow-wrap: anywhere; "
                            "word-break: break-word; max-width: 100%;"
                        ),
                    ),
                    title=_("Update not started"),
                    easy_close=True,
                    footer=ui.div(
                        ui.input_action_button("btn_modal_cancel", _("Close")),
                    ),
                )
            )
            update_progress_watcher.modal_open = True
            update_progress_watcher.dismiss_dialog_shown = True

            # Show reboot dialog with error if update failed, but only once
        elif (
            state["result"] == "error"
            and state.get("reboot_required", True)
            and not update_progress_watcher.reboot_dialog_shown
        ):
            ui.modal_remove()
            set_update_progress(result="reboot_dialog", in_progress=False)
            error_text = str(state.get("error_msg") or "").strip()
            ui.modal_show(
                ui.modal(
                    ui.div(
                        ui.markdown(
                            _(
                                "The update could not be completed. Review the error below, then restart the Kittyflap to recover."
                            )
                        ),
                        ui.div(
                            error_text,
                            style_=(
                                "margin-top: 0.5em; padding: 0.75em; "
                                "border: 1px solid var(--bs-border-color, #dee2e6); "
                                "border-radius: 0.375rem; "
                                "background: var(--bs-tertiary-bg, #f8f9fa); "
                                "color: var(--bs-body-color, #212529); "
                                "font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; "
                                "font-size: 0.9em; white-space: pre-wrap; "
                                "overflow-wrap: anywhere; word-break: break-word; max-width: 100%;"
                            ),
                        ),
                        ui.br(),
                        ui.markdown(
                            _(
                                "After the restart, check the version and logs before trying the update again."
                            )
                        ),
                    ),
                    title=_("Restart required"),
                    easy_close=False,
                    footer=ui.div(
                        ui.input_action_button("btn_modal_reboot_ok", _("Restart")),
                    ),
                )
            )
            update_progress_watcher.modal_open = True
            update_progress_watcher.reboot_dialog_shown = True

            # If modal is open but result is not reboot or update, close it
        elif (
            update_progress_watcher.modal_open
            and state["result"] not in ("ok", "reboot_dialog", "error")
            and not state["in_progress"]
        ):
            ui.modal_remove()
            update_progress_watcher.modal_open = False
            update_progress_watcher.reboot_dialog_shown = False
            update_progress_watcher.dismiss_dialog_shown = False
