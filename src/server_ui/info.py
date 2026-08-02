"""Info tab, backups, changelogs, update flow."""

import os
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
from src.baseconfig import CONFIG, set_language, get_loggable_config_value
from src.helper import (
    SystemInfo,
    Versioning,
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

_ = set_language(CONFIG["LANGUAGE"])

if is_remote_mode():
    from src.remote.hardware import Magnets, Pir  # type: ignore
else:
    from src.magnets_rfid import Magnets
    from src.pir import Pir


def register_info(input, output, session, ctx: SessionContext):
    """Register Info tab handlers (logs, backups, changelogs, update)."""

    @render.download(filename="kittyhack_logs.zip")
    def download_logfile():
        # Show a modal dialog to inform the user that the download is in progress
        ui.modal_show(
            ui.modal(
                ui.div(
                    ui.markdown(_("Creating log archive. Please wait...")),
                    ui.HTML(
                        '<div class="spinner-container"><div class="spinner"></div></div>'
                    ),
                ),
                title=_("Preparing Download"),
                easy_close=False,
                footer=None,
            )
        )

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

                        # Create a zip file with compression and include only the journal and sanitized config + setup logs
            with zipfile.ZipFile(
                zip_file_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as z:
                # Add the journalctl export
                if os.path.exists(journal_file_path):
                    z.write(journal_file_path, arcname="journalctl.log")

                    # Add target-mode journal when available (remote-mode only).
                if os.path.exists(target_journal_file_path):
                    z.write(target_journal_file_path, arcname="journalctl_target.log")

                    # Add a sanitized version of config.ini to the zip file
                sanitized_config_path = os.path.join(temp_dir, "config_sanitized.ini")

                # Create a sanitized version of the config using the CONFIG dictionary
                with open(sanitized_config_path, "w") as sanitized_file:
                    sanitized_file.write("[Settings]\n")
                    for key, value in CONFIG.items():
                        # Use the existing function to mask sensitive values
                        loggable_value = get_loggable_config_value(key, value)
                        sanitized_file.write(f"{key.lower()} = {loggable_value}\n")

                        # Add the sanitized config to the zip file
                z.write(sanitized_config_path, arcname="config_sanitized.ini")

                # Include a consistent kittyhack.db snapshot (same approach as download_kittyhack_db)
                db_arcname = "kittyhack_database.db"
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

            ui.modal_remove()
            return zip_file_path
        except Exception as e:
            logging.error(f"Failed to create logs zip file: {e}")
            ui.notification_show(
                _("Failed to create logs zip file: {}").format(e),
                duration=10,
                type="error",
            )
            ui.modal_remove()
            return None

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
                        "Database snapshot is unavailable during image migration ({} pictures remaining). Please try again later."
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
                    _("Failed to create a consistent snapshot: {}").format(
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
                _("Failed to prepare database download: {}").format(e),
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
                _("The original kittyflap database file does not exist."),
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
                CONFIG["LATEST_VERSION"] = Versioning.read_latest_kittyhack_version(
                    timeout=5
                )
                if CONFIG["LATEST_VERSION"] == "unknown":
                    ui.notification_show(
                        _("Latest version check failed."), duration=8, type="warning"
                    )
            else:
                ui.notification_show(
                    _("Internet connection not available."), duration=8, type="warning"
                )
        except Exception as e:
            logging.warning(f"[VERSION] Retry failed: {e}")
            ui.notification_show(
                _("Retry failed: {}").format(e), duration=10, type="error"
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
                    "Remote and Kittyflap versions differ (this device: {}, target device: {}). Please update to match versions."
                ).format(startup.git_version, target_git_version),
                duration=20,
                type="warning",
            )
            ctx.version_mismatch_warning_shown = True
        elif (not versions_mismatch) and ctx.version_mismatch_warning_shown:
            ctx.version_mismatch_warning_shown = False

            # Check if the current version is different from the latest version
        latest_version = CONFIG["LATEST_VERSION"]
        if latest_version == "unknown":
            ui_update_kittyhack = ui.div(
                ui.markdown(
                    _(
                        "Unable to fetch the latest version from github. Please try it again later or check your internet connection."
                    )
                ),
                ui.br(),
                ui.div(
                    ui.input_task_button(
                        "btn_retry_latest_version",
                        _("Retry latest version check"),
                        icon=icon_svg("rotate"),
                    ),
                    style_="text-align: center;",
                ),
            )

            if versions_mismatch and startup.git_repo_available:
                ui_update_kittyhack = (
                    ui_update_kittyhack,
                    ui.hr(),
                    ui.div(
                        ui.markdown(
                            _("Remote-/Kittyflap-version mismatch detected.")
                            + " "
                            + _(
                                "You can update only one side to match versions before running a full update."
                            )
                        ),
                        ui.div(
                            ui.input_task_button(
                                "update_target_kittyhack",
                                _("Update target device only"),
                                icon=icon_svg("download"),
                                class_="btn-default",
                            ),
                            ui.br(),
                            ui.br(),
                            ui.input_task_button(
                                "update_remote_kittyhack",
                                _("Update this device only"),
                                icon=icon_svg("download"),
                                class_="btn-default",
                            ),
                            style_="text-align: center;",
                        ),
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
                        "**" + _("Release Notes for") + " " + latest_version + ":**"
                    ),
                    ui.div(ui.markdown(release_notes), class_="release_notes"),
                    ui.br(),
                )
            except Exception as e:
                logging.warning(f"[VERSION] Failed to fetch release notes: {e}")
                release_notes_block = ui.div(
                    ui.markdown(_("Release notes unavailable: {}").format(e)), ui.br()
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
                        ),
                        ui.br(),
                        ui.help_text(
                            _(
                                "Important: A stable WLAN connection is required for the update process."
                            )
                        ),
                        ui.br(),
                        ui.help_text(
                            _("The update will end with a reboot of the Kittyflap.")
                        ),
                        ui.markdown(
                            _(
                                "Check out the [Changelog](https://github.com/floppyFK/kittyhack/releases) to see what's new in the latest version."
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
                                _("Remote-/Kittyflap-version mismatch detected.")
                                + " "
                                + _(
                                    "Use one of the buttons below if you want to update only one device."
                                )
                            ),
                            ui.div(
                                ui.input_task_button(
                                    "update_target_kittyhack",
                                    _("Update target device only"),
                                    icon=icon_svg("download"),
                                    class_="btn-default",
                                ),
                                ui.br(),
                                ui.br(),
                                ui.input_task_button(
                                    "update_remote_kittyhack",
                                    _("Update this device only"),
                                    icon=icon_svg("download"),
                                    class_="btn-default",
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
                                "This installation does not appear to be a git clone. Automatic updates require a git repository (git clone)."
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
                                        "WARNING: Local changes detected in the git repository in `{}`."
                                    ).format(kittyhack_root())
                                    + "\n\n"
                                    + _(
                                        "If you proceed with the update, these changes will be lost (the database and configuration will not be affected)."
                                    )
                                    + "\n\n"
                                    + _(
                                        "Please commit or stash your changes manually before updating, if you want to keep them."
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
                                _("Unable to check local git changes: {}").format(e)
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
                        ui.markdown(_("Remote-/Kittyflap-version mismatch detected.")),
                        ui.div(
                            ui.input_task_button(
                                "update_target_kittyhack",
                                _("Update target device only"),
                                icon=icon_svg("download"),
                                class_="btn-default",
                            ),
                            ui.br(),
                            ui.br(),
                            ui.input_task_button(
                                "update_remote_kittyhack",
                                _("Update this device only"),
                                icon=icon_svg("download"),
                                class_="btn-default",
                            ),
                            style_="text-align: center;",
                        ),
                    ),
                )

                # Force-update block: always offer to re-run the update, regardless of
                # version-equality. Useful when tracking a branch (commits may change
                # without the version string moving) or when testing a fork where the
                # latest release tag equals the currently installed one.
        if startup.git_repo_available:
            custom_repo_mode = (
                str(CONFIG.get("UPDATE_REPOSITORY_MODE") or "standard").strip().lower()
                == "custom"
            )
            force_update_help = (
                _(
                    "The current update source is your custom repository. Use this button to pull the latest commit on the selected branch or re-install the selected tag."
                )
                if custom_repo_mode
                else _(
                    "Re-install the currently selected version from the configured update source."
                )
            )
            ui_update_kittyhack = (
                ui_update_kittyhack,
                ui.hr(),
                ui.div(
                    ui.h6(_("Force update")),
                    ui.help_text(force_update_help),
                    ui.br(),
                    ui.div(
                        ui.input_task_button(
                            "btn_force_update_kittyhack",
                            _("Force update now"),
                            icon=icon_svg("rotate"),
                            class_="btn-default",
                        ),
                        style_="text-align: center;",
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
                        "The original kittyflap database file consumes currently **{:.1f} MB** of disk space."
                    ).format(SystemInfo.get_file_size(CONFIG["DATABASE_PATH"]))
                    + "\n\n"
                    + _(
                        "The file contains a lot pictures which could not be uploaded to the original kittyflap servers anymore."
                    )
                    + "\n\n"
                    + _("You could delete the pictures from it to free up disk space.")
                ),
                ui.input_task_button(
                    "clear_kittyflap_db",
                    _("Remove pictures from original Kittyflap Database"),
                    icon=icon_svg("trash"),
                ),
                ui.download_button(
                    "download_kittyflap_db",
                    _("Download Kittyflap Database"),
                    icon=icon_svg("download"),
                ),
            )

            # Build the whole section (card) only if the file exists
            ui_kittyflap_section = ui.div(
                ui.card(
                    ui.card_header(
                        ui.h4(
                            _("Original Kittyflap Database"),
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
                            "Kittyhack is an open-source project that enables offline use of the Kittyflap cat door—completely without internet access."
                        )
                        + "\n\n"
                        + _(
                            "It was created after the manufacturer of Kittyflap filed for bankruptcy, rendering the associated app non-functional."
                        )
                    ),
                    ui.hr(),
                    ui.markdown(
                        _("**Important Notes**")
                        + "\n\n"
                        + _(
                            "I have no connection to the manufacturer of Kittyflap. This project was developed on my own initiative to continue using my Kittyflap."
                        )
                        + "\n\n"
                        + _(
                            "If you find any bugs or have suggestions for improvement, please report them on the GitHub page."
                        )
                    ),
                    ui.HTML(
                        "<center><p><a href='https://github.com/floppyFK/kittyhack' target='_blank'>"
                        + str(icon_svg("square-github"))
                        + " "
                        + _("GitHub Repository")
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
                            "If you like Kittyhack, you can support the project with a small donation:"
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
                        ui.h4(_("Version Information"), style_="text-align: center;")
                    ),
                    ui.br(),
                    ui.markdown(
                        "**"
                        + _("Current Version")
                        + ":** `"
                        + startup.git_version
                        + "`"
                        + "  \n"
                        + (
                            "**"
                            + _("Current Version on Kittyflap")
                            + ":** `"
                            + target_git_version
                            + "`"
                            + "  \n"
                            if is_remote_mode()
                            else ""
                        )
                        + "**"
                        + _("Latest Version")
                        + ":** `"
                        + latest_version
                        + "`"
                    ),
                    (
                        ui.div(
                            ui.markdown(
                                f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} "
                                + _(
                                    "The remote and Kittyflap versions differ. This may lead to malfunction during operation."
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
                            _("Show all Changelogs"),
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
                        ui.h4(_("System Information"), style_="text-align: center;")
                    ),
                    ui.br(),
                    ui.output_ui("ui_system_info"),
                    ui.hr(),
                    ui.h5(_("Kittyhack Database")),
                    ui.h6(_("Backup and Restore your Kittyhack database")),
                    ui.div(
                        ui.download_button(
                            "download_kittyhack_db",
                            _("Download Kittyhack Database"),
                            icon=icon_svg("download"),
                        )
                    ),
                    ui.br(),
                    # Group: Restore Kittyhack DB
                    ui.div(
                        ui.div(
                            uix.input_file(
                                "upload_kittyhack_db",
                                _("Restore Kittyhack Database (.db)"),
                                accept=[".db"],
                                multiple=False,
                                width="90%",
                            ),
                            ui.div(
                                ui.input_task_button(
                                    "restore_kittyhack_db",
                                    _("Restore Database"),
                                    icon=icon_svg("rotate"),
                                    class_="btn-outline-danger",
                                ),
                                style_="text-align: center; margin-top: 6px;",
                            ),
                            class_="generic-container",
                            style_=(
                                "border: 1px solid #ddd; border-radius: 6px; padding: 10px; margin-top: 8px;"
                                "background: #fafafa;"
                            ),
                        )
                    ),
                    ui.hr(),
                    ui.h5(_("Configuration File")),
                    ui.h6(_("Backup and Restore your Kittyhack configuration")),
                    ui.div(
                        ui.download_button(
                            "download_config",
                            _("Download Configuration File"),
                            icon=icon_svg("download"),
                        )
                    ),
                    ui.br(),
                    # Group: Restore Configuration
                    ui.div(
                        ui.div(
                            uix.input_file(
                                "upload_config",
                                _("Restore Configuration File (config.ini)"),
                                accept=[".ini"],
                                multiple=False,
                                width="90%",
                            ),
                            ui.div(
                                ui.input_task_button(
                                    "restore_config",
                                    _("Restore Configuration"),
                                    icon=icon_svg("rotate"),
                                    class_="btn-outline-danger",
                                ),
                                style_="text-align: center; margin-top: 6px;",
                            ),
                            class_="generic-container",
                            style_=(
                                "border: 1px solid #ddd; border-radius: 6px; padding: 10px; margin-top: 8px;"
                                "background: #fafafa;"
                            ),
                        )
                    ),
                    ui.br(),
                    ui.markdown(
                        _(
                            "> Note: If the database or the configuration gets restored from a backup, then the Kittyflap will be rebooted afterwards to apply the new configuration."
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
                        + _("on your Smartphone or computer for easier access.")
                        + "  \n"
                        + _(
                            "Such a PWA can be added to your home screen and launched like a native app, without needing to open a web browser."
                        )
                    ),
                    ui.div(
                        # HTTPS Warning message
                        ui.div(
                            ui.markdown(
                                f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} "
                                + _("**HTTPS Required**")
                                + "\n\n"
                                + _(
                                    "PWA installation requires a secure connection (HTTPS). You are currently accessing Kittyhack via HTTP."
                                )
                                + "  \n\n"
                                + _("You'll need to set up a reverse proxy with HTTPS.")
                                + " "
                                + _(
                                    "If you want to setup a reverse proxy in your home network, you can watch"
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
                                + _("**App is already installed on this device!**")
                            ),
                            id="pwa_already_installed",
                            style_="display: none; color: #1cc88a; padding: 10px; text-align: center;",
                        ),
                        # Success message
                        ui.div(
                            ui.markdown(
                                f"{icon_svg('circle-check', margin_left='-0.1em')} "
                                + _("**Installation successful!**")
                            ),
                            id="pwa_installed_success",
                            style_="display: none; color: #1cc88a; padding: 10px; text-align: center;",
                        ),
                        # Install button
                        ui.div(
                            ui.input_action_button(
                                id="pwa_install_button",
                                label=_("Install as App"),
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
                    ui.card_header(ui.h4(_("Logfiles"), style_="text-align: center;")),
                    ui.br(),
                    ui.div(
                        ui.download_button(
                            "download_logfile",
                            _("Download Kittyhack Logfile"),
                            icon=icon_svg("download"),
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
            wlan_info = _("Not available in remote-mode")
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
                wlan_info = _("Unable to determine")

                # Create a DataFrame with the system information
        df = pd.DataFrame(
            {
                "Property": [
                    _("Kittyhack database size"),
                    _("Free disk space"),
                    _("Used RAM"),
                    _("WLAN Status"),
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
        changelog_text = Versioning.get_changelogs(
            after_version="v1.0.0", language=CONFIG["LANGUAGE"]
        )
        ui.modal_show(
            ui.modal(
                ui.div(
                    ui.markdown(changelog_text),
                ),
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
            p.set(1, message="Deleting the pictures", detail="This may take a while...")
            if os.path.exists(CONFIG["DATABASE_PATH"]):
                try:
                    DbMigrations.clear_original_kittyflap_database(
                        CONFIG["DATABASE_PATH"]
                    )
                    ui.notification_show(
                        _(
                            "The pictures from the original kittyflap database were removed successfully."
                        ),
                        duration=5,
                        type="message",
                    )
                    p.set(2)
                except Exception as e:
                    ui.notification_show(
                        _(
                            "An error occurred while deleting the pictures from the original kittyflap database: {}"
                        ).format(e),
                        duration=10,
                        type="error",
                    )
            else:
                ui.notification_show(
                    _("The original kittyflap database file does not exist anymore."),
                    duration=5,
                    type="message",
                )
        ctx.reload_trigger_info.set(ctx.reload_trigger_info.get() + 1)

    @render.download(filename="config.ini")
    def download_config():
        try:
            # Simply return the path to the actual config.ini file
            config_ini_path = os.path.join(os.getcwd(), "config.ini")

            if os.path.exists(config_ini_path):
                return config_ini_path
            else:
                logging.error("config.ini not found")
                ui.notification_show(
                    _("config.ini not found"), duration=10, type="error"
                )
                return None
        except Exception as e:
            logging.error(f"Failed to download config.ini file: {e}")
            ui.notification_show(
                _("Failed to download config.ini file: {}").format(e),
                duration=10,
                type="error",
            )
            return None

    @reactive.Effect
    @reactive.event(input.upload_kittyhack_db)
    def on_upload_kittyhack_db():
        files = input.upload_kittyhack_db()
        if not files:
            ui.notification_show(_("No file selected."), duration=8, type="error")
            ctx.last_uploaded_db_path.set(None)
            return
        f = files[0]
        name = f.get("name", "")
        src_path = f.get("datapath", "")
        if not src_path or not os.path.exists(src_path):
            ui.notification_show(
                _("Uploaded file not found."), duration=8, type="error"
            )
            ctx.last_uploaded_db_path.set(None)
            return
        if not name.lower().endswith(".db"):
            ui.notification_show(
                _("Invalid file type. Please upload a .db file."),
                duration=10,
                type="error",
            )
            ctx.last_uploaded_db_path.set(None)
            return
            # Only store path and inform user; do not restore yet
        ctx.last_uploaded_db_path.set(src_path)
        ui.notification_show(
            _("Database file uploaded. Click 'Restore Database' to apply."),
            duration=8,
            type="message",
        )

    @reactive.Effect
    @reactive.event(input.restore_kittyhack_db)
    def on_restore_kittyhack_db():
        src_path = ctx.last_uploaded_db_path.get()
        if not src_path or not os.path.exists(src_path):
            ui.notification_show(
                _("No uploaded database ready to restore."), duration=8, type="error"
            )
            return
        try:
            # Stop backend to avoid DB locks
            sigterm_monitor.halt_backend()
            tm.sleep(1.0)
            # Backup current DB before overwrite
            backup_dir = os.path.dirname(CONFIG["KITTYHACK_DATABASE_PATH"]) or "."
            backup_name = (
                f"kittyhack_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            )
            backup_dest = os.path.join(backup_dir, backup_name)
            try:
                shutil.copy2(CONFIG["KITTYHACK_DATABASE_PATH"], backup_dest)
                logging.info(f"[UPLOAD_DB] Backup created: {backup_dest}")
            except Exception as e:
                logging.warning(f"[UPLOAD_DB] Failed to create backup: {e}")
                # Overwrite DB with uploaded file
            shutil.copy2(src_path, CONFIG["KITTYHACK_DATABASE_PATH"])
            ui.notification_show(
                _("Database restored successfully."), duration=6, type="message"
            )
            # Prompt reboot to apply
            logging.info("Database restore performed --> Restart pending.")
            m = ui.modal(
                ui.markdown(
                    _("Please click the 'Reboot' button to restart the Kittyflap.")
                ),
                title=_("Reboot required"),
                easy_close=False,
                footer=ui.div(
                    ui.input_action_button("btn_modal_reboot_ok", _("Reboot")),
                ),
            )
            ui.modal_show(m)
        except Exception as e:
            logging.error(f"[RESTORE_DB] Failed to restore database: {e}")
            ui.notification_show(
                _("Failed to restore database: {}").format(e), duration=12, type="error"
            )

    @reactive.Effect
    @reactive.event(input.upload_config)
    def on_upload_config():
        files = input.upload_config()
        if not files:
            ui.notification_show(_("No file selected."), duration=8, type="error")
            ctx.last_uploaded_cfg_path.set(None)
            return
        f = files[0]
        name = f.get("name", "")
        src_path = f.get("datapath", "")
        if not src_path or not os.path.exists(src_path):
            ui.notification_show(
                _("Uploaded file not found."), duration=8, type="error"
            )
            ctx.last_uploaded_cfg_path.set(None)
            return
        if not name.lower().endswith(".ini"):
            ui.notification_show(
                _("Invalid file type. Please upload a config.ini file."),
                duration=10,
                type="error",
            )
            ctx.last_uploaded_cfg_path.set(None)
            return
            # Optional quick validation
        try:
            with open(src_path, "r", encoding="utf-8", errors="ignore") as fcfg:
                content = fcfg.read()
            if "[Settings]" not in content:
                ui.notification_show(
                    _("Invalid configuration file: missing [Settings] section."),
                    duration=12,
                    type="error",
                )
                ctx.last_uploaded_cfg_path.set(None)
                return
        except Exception as e:
            ui.notification_show(
                _("Failed to validate configuration: {}").format(e),
                duration=10,
                type="error",
            )
            ctx.last_uploaded_cfg_path.set(None)
            return
            # Only store path and inform user; do not restore yet
        ctx.last_uploaded_cfg_path.set(src_path)
        ui.notification_show(
            _("Configuration file uploaded. Click 'Restore Configuration' to apply."),
            duration=8,
            type="message",
        )

    @reactive.Effect
    @reactive.event(input.restore_config)
    def on_restore_config():
        src_path = ctx.last_uploaded_cfg_path.get()
        if not src_path or not os.path.exists(src_path):
            ui.notification_show(
                _("No uploaded configuration ready to restore."),
                duration=8,
                type="error",
            )
            return
        try:
            config_path = os.path.join(os.getcwd(), "config.ini")
            shutil.copy2(src_path, config_path)
            ui.notification_show(
                _("Configuration restored successfully."), duration=6, type="message"
            )
            logging.info("Configuration restore performed --> Restart pending.")
            m = ui.modal(
                ui.markdown(
                    _("Please click the 'Reboot' button to restart the Kittyflap.")
                ),
                title=_("Reboot required"),
                easy_close=False,
                footer=ui.div(
                    ui.input_action_button("btn_modal_reboot_ok", _("Reboot")),
                ),
            )
            ui.modal_show(m)
        except Exception as e:
            logging.error(f"[RESTORE_CFG] Failed to restore configuration: {e}")
            ui.notification_show(
                _("Failed to restore configuration: {}").format(e),
                duration=12,
                type="error",
            )

    def _start_update_process(update_target: bool, update_local: bool):
        latest_version = CONFIG["LATEST_VERSION"]
        current_version = startup.git_version

        if not startup.git_repo_available:
            ui.notification_show(
                _(
                    "Automatic update requires a git repository. Please reinstall via git clone or the setup script."
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
                _("Target-only update is only available in remote-mode."),
                duration=8,
                type="error",
            )
            return

        if is_remote_mode() and update_target and update_local:
            initial_max_steps = 9
        elif update_local:
            initial_max_steps = 8
        else:
            initial_max_steps = 1

        set_update_progress(
            in_progress=True,
            step=1,
            max_steps=initial_max_steps,
            message=_("Starting update..."),
            detail="",
            result=None,
            error_msg="",
        )

        # Start the update in a background thread so the UI can update immediately
        def run_update():
            remote_mode_active = bool(is_remote_mode())

            def _set_error_and_stop(message: str):
                logging.error(f"Kittyhack update failed: {message}")
                set_update_progress(
                    result="error", in_progress=False, error_msg=message
                )

            if remote_mode_active and update_target:
                try:
                    from src.remote.control_client import RemoteControlClient

                    set_update_progress(
                        in_progress=True,
                        step=1,
                        max_steps=(9 if update_local else 1),
                        message=_("Updating connected target device..."),
                        detail=_("Preparing remote update..."),
                        result=None,
                        error_msg="",
                    )

                    client = RemoteControlClient.instance()
                    client.ensure_started()
                    if not client.wait_until_ready(timeout=20.0):
                        _set_error_and_stop(_("Remote target is not connected."))
                        return

                    client.start_target_update(
                        latest_version=latest_version, current_version=current_version
                    )
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
                                _("Timed out while waiting for target device update.")
                            )
                            return

                        status = client.get_target_update_status()
                        if bool(status.get("in_progress")):
                            had_in_progress_state = True

                        if status.get("ok") is False:
                            reason = str(
                                status.get("reason")
                                or _("Unknown target update error.")
                            )
                            _set_error_and_stop(
                                _("Target device update failed: {}.").format(reason)
                            )
                            return
                        if status.get("ok") is True:
                            set_update_progress(
                                in_progress=True,
                                step=1,
                                max_steps=(9 if update_local else 1),
                                message=_("Connected target device updated."),
                                detail=(
                                    _("Starting update on this device...")
                                    if update_local
                                    else _("Update finished.")
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
                                message=_("Updating connected target device..."),
                                detail=_(
                                    "Connection lost while target is restarting/reconnecting. Waiting..."
                                ),
                                result=None,
                                error_msg="",
                            )
                            if elapsed >= reconnect_grace_s:
                                _set_error_and_stop(
                                    _(
                                        "Connection to target device did not recover in time after update restart."
                                    )
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
                                                "Connected target device updated."
                                            ),
                                            detail=(
                                                _("Starting update on this device...")
                                                if update_local
                                                else _("Update finished.")
                                            ),
                                            result=None,
                                            error_msg="",
                                        )
                                        break

                        if status.get("in_progress"):
                            detail = _("Target update is running...")
                        elif status.get("requested"):
                            detail = _("Waiting for target update to start...")
                        else:
                            detail = _("Waiting for remote target response...")

                        set_update_progress(
                            in_progress=True,
                            step=1,
                            max_steps=(9 if update_local else 1),
                            message=_("Updating connected target device..."),
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
                                            "Target device reconnected, but version is still {} instead of {}."
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
                    _set_error_and_stop(_("Failed to run target update: {}.").format(e))
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
    @reactive.event(input.update_kittyhack)
    def update_kittyhack_process():
        # In remote-mode: target first, then local device.
        # In target/local mode: local device only.
        _start_update_process(update_target=bool(is_remote_mode()), update_local=True)

    @reactive.Effect
    @reactive.event(input.update_target_kittyhack)
    def update_target_kittyhack_process():
        _start_update_process(update_target=True, update_local=False)

    @reactive.Effect
    @reactive.event(input.update_remote_kittyhack)
    def update_remote_kittyhack_process():
        _start_update_process(update_target=False, update_local=True)

    @reactive.Effect
    @reactive.event(input.btn_force_update_kittyhack)
    def force_update_kittyhack_process():
        # Same flow as the regular update button; the underlying KittyhackUpdater.update_kittyhack()
        # already handles both tag mode (re-checkout) and branch mode (reset to
        # origin/<ref>) correctly when called with the current latest_version.
        _start_update_process(update_target=bool(is_remote_mode()), update_local=True)

    @reactive.Effect
    @reactive.event(input.btn_modal_update_repo_now)
    def _on_modal_update_repo_now():
        ui.modal_remove()
        _start_update_process(update_target=bool(is_remote_mode()), update_local=True)

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
                            <div style="color: #888;">
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
                        _("Do not close this page until the update is finished!")
                    ),
                    ui.markdown(
                        _("*This step may take several minutes. Please be patient.*")
                    ),
                ),
                title=_("Updating Kittyhack..."),
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

            # Show update progress modal only when starting update
        if state["in_progress"] and not update_progress_watcher.modal_open:
            show_update_progress_modal()
            update_progress_watcher.modal_open = True
            update_progress_watcher.reboot_dialog_shown = False

            # Remove modal and show result only when update is finished
        elif (
            update_progress_watcher.modal_open
            and not state["in_progress"]
            and state["result"] is None
        ):
            ui.modal_remove()
            update_progress_watcher.modal_open = False
            update_progress_watcher.reboot_dialog_shown = False

            # Show reboot dialog if update finished and reboot required, but only once
        elif (
            state["result"] == "ok" or state["result"] == "reboot_dialog"
        ) and not update_progress_watcher.reboot_dialog_shown:
            # Always remove any open modal first
            ui.modal_remove()
            set_update_progress(result="reboot_dialog", in_progress=False)
            ui.modal_show(
                ui.modal(
                    _(
                        "A restart is required to apply the update. Please click the 'Reboot' button to restart the Kittyflap."
                    ),
                    title=_("Restart required"),
                    easy_close=False,
                    footer=ui.div(
                        ui.input_action_button("btn_modal_reboot_ok", _("Reboot")),
                    ),
                )
            )
            update_progress_watcher.modal_open = True
            update_progress_watcher.reboot_dialog_shown = True

            # Show reboot dialog with error if update failed, but only once
        elif (
            state["result"] == "error"
            and not update_progress_watcher.reboot_dialog_shown
        ):
            ui.modal_remove()
            set_update_progress(result="reboot_dialog", in_progress=False)
            ui.modal_show(
                ui.modal(
                    ui.div(
                        ui.markdown(_("An error occurred during the update process:")),
                        ui.markdown(f"```\n{state['error_msg']}\n```"),
                        ui.br(),
                        ui.markdown(
                            _(
                                "A restart is required to recover. Please click the 'Reboot' button to restart the Kittyflap."
                            )
                        ),
                    ),
                    title=_("Restart required"),
                    easy_close=False,
                    footer=ui.div(
                        ui.input_action_button("btn_modal_reboot_ok", _("Reboot")),
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
