"""Kittyhack SQLite access.

Public API is organized into classes (`DatabaseCore`, `EventsRepo`, `CatsRepo`,
`DbMigrations`). Module-level aliases preserve existing imports such as
`from src.database import *` and `CatsRepo.db_get_cats(...)`.
"""
import os

from dataclasses import dataclass

import pandas as pd

import sqlite3

from enum import Enum

from threading import Lock

import logging

import sys

import cv2

import time as tm

import base64

import numpy as np

import shutil

import json

from datetime import datetime, time, timezone

from src.baseconfig import CONFIG, update_single_config_parameter

from typing import TypedDict, List, Iterable

from src.helper import (
    DateTimeUtil,
    ImageUtil,
    Result,
)

from src.camera import image_buffer, DetectedObject

from src.paths import pictures_original_dir, pictures_thumbnails_dir

ORIGINAL_IMAGE_DIR = pictures_original_dir()

THUMBNAIL_DIR = pictures_thumbnails_dir()

os.makedirs(ORIGINAL_IMAGE_DIR, exist_ok=True)

os.makedirs(THUMBNAIL_DIR, exist_ok=True)

EVENT_BUNDLE_DIR = os.path.join(THUMBNAIL_DIR, "bundles")

os.makedirs(EVENT_BUNDLE_DIR, exist_ok=True)

class db_action(Enum):
    """Legacy action codes for older DB query helpers."""
    get_photos = 0
    get_photos_table = 1
    get_photos_ids = 2
    get_cats = 3
    get_cats_table = 4
    get_config = 5
    get_config_table = 6

class ReturnDataPhotosDB(Enum):
    """Which columns/blobs to include when reading events/photos."""
    all = 0
    all_except_photos = 1
    only_ids = 2
    all_modified_image = 3
    all_original_image = 4

class ReturnDataCatDB(Enum):
    """Which columns to include when reading cats."""
    all = 0
    all_except_photos = 1

class ReturnDataConfigDB(Enum):
    """Which columns to include when reading legacy kittyflap config."""
    all = 0
    all_except_password = 1

class DetectedObjectSchema(TypedDict):
    """JSON-serializable detection box stored inside event_text."""
    object_name: str
    probability: float
    x: float
    y: float
    width: float
    height: float

class EventSchema(TypedDict):
    """JSON shape persisted in events.event_text."""
    detected_objects: List[DetectedObjectSchema]
    event_text: str

class LastImageBlockTimestamp:
    """Process-wide timestamp of the last motion block written to the DB."""
    _timestamp = tm.time()
    _lock = Lock()

    @classmethod
    def get_timestamp(cls):
        """Return the last written motion-block unix timestamp."""
        with cls._lock:
            return cls._timestamp

    @classmethod
    def update_timestamp(cls, timestamp: float):
        """Update the last written motion-block unix timestamp."""
        with cls._lock:
            cls._timestamp = timestamp

last_imgblock_ts = LastImageBlockTimestamp()

_cat_thumbnail_cache = {}

db_write_lock = Lock()

class DatabaseCore:
    """Low-level SQLite lock/read/write, schema checks, vacuum, backup, integrity."""

    @staticmethod
    def lock_database(timeout: int = 60, check_interval: float = 0.1) -> Result:
        """Acquire ``db_write_lock``, waiting up to ``timeout`` seconds."""
        start_time = tm.time()
        while tm.time() - start_time < timeout:
            if db_write_lock.acquire(blocking=False):
                logging.debug("[DATABASE] Database lock acquired.")
                return Result(True, "")
            tm.sleep(check_interval)
        error_message = f"[DATABASE] Database lock not released within the given timeout ({timeout}s)."
        logging.error(error_message)
        return Result(False, error_message)

    @staticmethod
    def release_database():
        """Release ``db_write_lock`` if currently held."""
        if db_write_lock.locked():
            db_write_lock.release()
            logging.debug("[DATABASE] Database lock released.")
        else:
            logging.warning("[DATABASE] Database lock is not acquired. Nothing to release.")

    @staticmethod
    def read_df_from_database(database: str, stmt: str) -> pd.DataFrame:
        """Run a SELECT under the DB lock and return a DataFrame (empty on error)."""
        result = DatabaseCore.lock_database()
        if not result.success:
            logging.error(f"[DATABASE] Failed to acquire lock for reading from database '{database}': {result.message}")
            return pd.DataFrame()

        try:
            conn = sqlite3.connect(database, timeout=30)
            df = pd.read_sql_query(stmt, conn)
            conn.close()
        except Exception as e:
            logging.error(f"[DATABASE] Failed to read from database '{database}': {e}")
            df = pd.DataFrame()
        else:
            logging.debug(f"[DATABASE] Read from database '{database}': {df}")
        finally:
            DatabaseCore.release_database()

        return df

    @staticmethod
    def read_column_info_from_database(database: str, table: str):
        """Return ``PRAGMA table_info`` rows for ``table`` (empty list on error)."""
        result = DatabaseCore.lock_database()
        if not result.success:
            logging.error(f"[DATABASE] Failed to acquire lock for reading column information from database '{database}': {result.message}")
            return []

        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA table_info({table})")
            columns_info = cursor.fetchall()
            conn.close()
        except Exception as e:
            logging.error(f"[DATABASE] Failed to read column information from database '{database}': {e}")
            columns_info = []
        else:
            logging.debug(f"[DATABASE] Read column information from database '{database}': {columns_info}")
        finally:
            DatabaseCore.release_database()

        return columns_info

    @staticmethod
    def write_stmt_to_database(database: str, stmt: str) -> Result:
        """Execute a write SQL statement under the DB lock."""
        result = DatabaseCore.lock_database()
        if not result.success:
            return result

        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute(stmt)
            conn.commit()
            conn.close()
        except Exception as e:
            error_message = f"[DATABASE] An error occurred while updating the database '{database}': {e}"
            logging.error(error_message)
            return Result(False, error_message)
        else:
            # success
            logging.debug(f"[DATABASE] Successfully executed statement to database '{database}': {stmt}")
            return Result(True, "")
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def db_get_config(database: str, return_data: ReturnDataConfigDB):
        """Read the legacy kittyflap ``config`` table as a DataFrame."""
        if return_data == ReturnDataConfigDB.all:
             columns = "*"
        elif return_data == ReturnDataConfigDB.all_except_password:
            columns = "id, wifi_ssid, ip, acceptance_rate, cat_prob_threshold, accept_all_cats, detect_prey"

        stmt = f"SELECT {columns} FROM config"
        return DatabaseCore.read_df_from_database(database, stmt)

    @staticmethod
    def db_set_config(database: str, 
                      updated_at: datetime, 
                      acceptance_rate: float, 
                      accept_all_cats: bool, 
                      detect_prey: bool, 
                      cat_prob_threshold: float) -> Result:
        """Update the single legacy kittyflap ``config`` row."""
        data = f"updated_at = '{updated_at}', acceptance_rate = {acceptance_rate}, accept_all_cats = {int(accept_all_cats)}, detect_prey = {int(detect_prey)}, cat_prob_threshold = {cat_prob_threshold}"
        logging.info(f"[DATABASE] Writing new kittyflap configuration to 'config' table in database '{database}': {data}")
        stmt = f"UPDATE config SET {data} WHERE id = (SELECT id FROM config LIMIT 1)"
        result = DatabaseCore.write_stmt_to_database(database, stmt)
        if result.success == True:
            logging.info("[DATABASE] Kittyflap configuration updated successfully.")
        return result

    @staticmethod
    def create_index_on_events(database: str) -> Result:
        """Create helpful indexes on ``events`` and ``cats`` if missing."""
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_id ON events (id)",
            "CREATE INDEX IF NOT EXISTS idx_block_id_created_at ON events (block_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_events_block_id_deleted_created_at ON events (block_id, deleted, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_cats_rfid ON cats (rfid)",
        ]

        for stmt in indexes:
            result = DatabaseCore.write_stmt_to_database(database, stmt)
            if not result.success:
                return result

        logging.info("[DATABASE] Successfully created indexes.")
        return Result(True, "")

    @staticmethod
    def vacuum_database(database: str) -> Result:
        """Run VACUUM + ANALYZE on the SQLite database."""
        result = DatabaseCore.lock_database()
        if not result.success:
            return result

        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute("VACUUM")
            cursor.execute("ANALYZE")
            conn.close()
        except Exception as e:
            error_message = f"[DATABASE] An error occurred while vacuuming and analyzing the database '{database}': {e}"
            logging.error(error_message)
            return Result(False, error_message)
        else:
            logging.info(f"[DATABASE] Successfully vacuumed and analyzed the database '{database}'.")
            return Result(True, "")
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def check_if_table_exists(database: str, table: str) -> bool:
        """True if ``table`` exists in the SQLite database."""
        if not os.path.exists(database):
            return False

        result = DatabaseCore.lock_database()
        if not result.success:
            logging.error(f"[DATABASE] Failed to acquire lock for checking if table '{table}' exists in the database '{database}': {result.message}")
            return False

        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
            result = cursor.fetchone()
            conn.close()
        except Exception as e:
            logging.error(f"[DATABASE] Failed to check if table '{table}' exists in the database '{database}': {e}")
            return False
        else:
            return True if result else False
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def check_if_column_exists(database: str, table: str, column: str) -> bool:
        """True if ``column`` exists on ``table``."""
        if not os.path.exists(database):
            return False

        result = DatabaseCore.lock_database()
        if not result.success:
            logging.error(f"[DATABASE] Failed to acquire lock for checking if column '{column}' exists in the table '{table}' of the database '{database}': {result.message}")
            return False

        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA table_info({table})")
            columns = cursor.fetchall()
            conn.close()
        except Exception as e:
            logging.error(f"[DATABASE] Failed to check if column '{column}' exists in the table '{table}' of the database '{database}': {e}")
            return False
        else:
            # Check if the column exists in the table
            column_names = [col[1] for col in columns]  # Column name is the second item in each row
            return column in column_names
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def add_column_to_table(database: str, table: str, column: str, column_type: str) -> Result:
        """ALTER TABLE to add ``column`` with SQLite ``column_type``."""
        if not os.path.exists(database):
            return Result(False, f"[DATABASE] Database '{database}' does not exist.")

        result = DatabaseCore.lock_database()
        if not result.success:
            return result

        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
            conn.commit()
            conn.close()
        except Exception as e:
            error_message = f"[DATABASE] An error occurred while adding column '{column}' to the table '{table}' of the database '{database}': {e}"
            logging.error(error_message)
            return Result(False, error_message)
        else:
            logging.info(f"[DATABASE] Successfully added column '{column}' to the table '{table}' of the database '{database}'.")
            return Result(True, "")
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def check_database_integrity(database: str, skip_lock: bool = False) -> Result:
        """Run SQLite ``PRAGMA integrity_check``; Result.success if ok."""
        if not skip_lock:
            result = DatabaseCore.lock_database()
            if not result.success:
                logging.error(f"[DATABASE] Failed to acquire lock for integrity check: {result.message}")
                return Result(False, result.message)

        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check")
            result = cursor.fetchone()
            conn.close()
        except Exception as e:
            error_message = f"[DATABASE] An error occurred while checking the integrity of the database '{database}': {e}"
            logging.error(error_message)
            return Result(False, error_message)
        else:
            if result[0] == 'ok':
                logging.info(f"[DATABASE] Database '{database}' integrity check passed.")
                return Result(True, "")
            else:
                error_message = f"[DATABASE] Database '{database}' integrity check failed: {result[0]}"
                logging.error(error_message)
                return Result(False, error_message)
        finally:
            if not skip_lock:
                DatabaseCore.release_database()

    @staticmethod
    def backup_database_sqlite(database: str, destination_path: str) -> Result:
        """Write a consistent SQLite backup of ``database`` to ``destination_path``."""
        try:
            # Use short lock window to avoid long blocking
            lock_res = DatabaseCore.lock_database()
            if not lock_res.success:
                logging.error(f"[DATABASE_BACKUP] Failed to lock database: {lock_res.message}")
                return Result(False, "database_locked")
            try:
                src = sqlite3.connect(database, timeout=30)
                dst = sqlite3.connect(destination_path, timeout=30)
                src.backup(dst)
                dst.close()
                src.close()
            except Exception as e:
                logging.error(f"[DATABASE_BACKUP] SQLite backup failed: {e}")
                # Clean up a potentially half-written file
                try:
                    if os.path.exists(destination_path):
                        os.remove(destination_path)
                except Exception as ex:
                    logging.warning(f"[DATABASE_BACKUP] Failed to remove incomplete backup: {ex}")
                return Result(False, "backup_failed")
            finally:
                DatabaseCore.release_database()

            logging.info(f"[DATABASE_BACKUP] Backup written to '{destination_path}'")
            return Result(True, "")
        except Exception as e:
            logging.error(f"[DATABASE_BACKUP] Unexpected error: {e}")
            return Result(False, "backup_failed")


class EventsRepo:
    """Events/photos/motion blocks, timelines, image files, and event JSON helpers."""

    @staticmethod
    def get_jpeg_size(jpeg_bytes: bytes | None) -> tuple[int, int] | None:
        """Return (width, height) from JPEG bytes without fully decoding the image."""
        if not jpeg_bytes or not isinstance(jpeg_bytes, (bytes, bytearray)):
            return None

        b = jpeg_bytes
        try:
            if len(b) < 4 or b[0] != 0xFF or b[1] != 0xD8:
                return None  # not a JPEG

            i = 2
            while i + 1 < len(b):
                # Find marker (0xFF ..)
                if b[i] != 0xFF:
                    i += 1
                    continue

                # Skip fill bytes 0xFF
                while i < len(b) and b[i] == 0xFF:
                    i += 1
                if i >= len(b):
                    break

                marker = b[i]
                i += 1

                # Standalone markers without length
                if marker in (0xD8, 0xD9):
                    continue
                if marker == 0xDA:
                    # Start of scan: image data follows, no more headers
                    break

                if i + 1 >= len(b):
                    break
                seg_len = (b[i] << 8) + b[i + 1]
                if seg_len < 2:
                    return None

                seg_start = i + 2
                seg_end = seg_start + (seg_len - 2)
                if seg_end > len(b):
                    break

                # SOF markers that contain size (baseline/progressive)
                if marker in (
                    0xC0, 0xC1, 0xC2, 0xC3,
                    0xC5, 0xC6, 0xC7,
                    0xC9, 0xCA, 0xCB,
                    0xCD, 0xCE, 0xCF,
                ):
                    if seg_start + 6 <= len(b):
                        # seg_start: precision (1), then height (2), width (2)
                        height = (b[seg_start + 1] << 8) + b[seg_start + 2]
                        width = (b[seg_start + 3] << 8) + b[seg_start + 4]
                        if width > 0 and height > 0:
                            return (int(width), int(height))

                i = seg_end
        except Exception:
            return None

        return None

    @staticmethod
    def update_image_dimensions_for_block(database: str, block_id: int, width: int, height: int) -> Result:
        """Store image dimensions for an event block if columns exist and values are missing."""
        try:
            if not DatabaseCore.check_if_column_exists(database, "events", "img_width") or not DatabaseCore.check_if_column_exists(database, "events", "img_height"):
                return Result(True, "")
        except Exception:
            return Result(True, "")

        result = DatabaseCore.lock_database()
        if not result.success:
            return result

        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE events SET img_width = ?, img_height = ? WHERE block_id = ? AND (img_width IS NULL OR img_height IS NULL)",
                (int(width), int(height), int(block_id)),
            )
            conn.commit()
            conn.close()
            return Result(True, "")
        except Exception as e:
            logging.warning(f"[DATABASE] Failed updating image dimensions for block_id {block_id}: {e}")
            return Result(False, str(e))
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def _event_bundle_paths(block_id: int) -> List[str]:
        """Return possible bundle paths for a block (supports legacy .tar.gz and current .tar)."""
        bid = int(block_id)
        return [
            os.path.join(EVENT_BUNDLE_DIR, f"event_{bid}.tar"),
            os.path.join(EVENT_BUNDLE_DIR, f"event_{bid}.tar.gz"),
        ]

    @staticmethod
    def _remove_event_bundle_files(block_ids: Iterable[int]) -> int:
        """Delete event bundle tar files for the given block IDs; return count removed."""
        removed = 0
        for bid in set(int(b) for b in block_ids if b is not None):
            for p in EventsRepo._event_bundle_paths(bid):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                        removed += 1
                        logging.debug(f"[DATABASE] Removed event bundle file '{p}'.")
                except Exception as e:
                    logging.warning(f"[DATABASE] Failed removing event bundle for block_id {bid} ('{p}'): {e}")
        return removed

    @staticmethod
    def _original_image_path(image_id: int) -> str:
        """Filesystem path for an event's original JPEG."""
        return os.path.join(ORIGINAL_IMAGE_DIR, f"{image_id}.jpg")

    @staticmethod
    def _thumbnail_image_path(image_id: int) -> str:
        """Filesystem path for an event's thumbnail JPEG."""
        return os.path.join(THUMBNAIL_DIR, f"{image_id}.jpg")

    @staticmethod
    def _remove_event_image_files(ids: Iterable[int]) -> tuple[int, int]:
        """Delete original/thumbnail JPGs for event IDs; return (orig_count, thumb_count)."""
        removed_orig = 0
        removed_thumb = 0
        for pid in set(int(i) for i in ids):
            # Original
            try:
                orig_path = EventsRepo._original_image_path(pid)
                if os.path.exists(orig_path):
                    os.remove(orig_path)
                    removed_orig += 1
                    logging.debug(f"[DATABASE] Removed original image file '{orig_path}'.")
            except Exception as e:
                logging.warning(f"[DATABASE] Failed removing original image file for ID {pid}: {e}")
            # Thumbnail
            try:
                thumb_path = EventsRepo._thumbnail_image_path(pid)
                if os.path.exists(thumb_path):
                    os.remove(thumb_path)
                    removed_thumb += 1
                    logging.debug(f"[DATABASE] Removed thumbnail file '{thumb_path}'.")
            except Exception as e:
                logging.warning(f"[DATABASE] Failed removing thumbnail file for ID {pid}: {e}")
        return removed_orig, removed_thumb

    @staticmethod
    def _make_placeholder_image(text: str, size=(640, 480), bg=(230, 230, 230), fg=(30, 30, 30)) -> bytes:
        """Build a simple JPEG placeholder with centered ``text``."""
        try:
            h, w = size[1], size[0]
            img = np.full((h, w, 3), bg, dtype=np.uint8)
            font = cv2.FONT_HERSHEY_SIMPLEX
            scale = 1.0
            thickness = 2
            line_type = cv2.LINE_AA
            # Split text into lines and center them
            lines = [text]
            y0 = h // 2 - 20 * (len(lines) - 1)
            for i, line in enumerate(lines):
                (tw, th), _ = cv2.getTextSize(line, font, scale, thickness)
                x = (w - tw) // 2
                y = y0 + i * (th + 12)
                cv2.putText(img, line, (x, y), font, scale, fg, thickness, line_type)
            ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if ok:
                return buf.tobytes()
        except Exception as e:
            logging.error(f"[PLACEHOLDER] Failed to build placeholder image: {e}")
        # Fallback: minimal empty JPEG header if something goes wrong
        return bytes([0xFF, 0xD8, 0xFF, 0xD9])

    @staticmethod
    def db_get_photos(database: str, 
                      return_data: ReturnDataPhotosDB, 
                      date_start="2020-01-01 00:00:00", 
                      date_end="2100-12-31 23:59:59", 
                      cats_only=False, 
                      mouse_only=False, 
                      mouse_probability=0.0, 
                      page_index = 0, 
                      elements_per_page = sys.maxsize,
                      ignore_deleted = True,
                      offset: int | None = None):
        """Query events/photos with filters and optional paging (newest first)."""
        # Discover optional columns once to keep queries compatible across schema versions.
        columns_info = DatabaseCore.read_column_info_from_database(database, "events")
        column_names = [info[1] for info in (columns_info or [])]
        fps_col = ", effective_fps" if ('effective_fps' in column_names) else ""

        if return_data == ReturnDataPhotosDB.all:
            columns = "id, block_id, created_at, event_type, original_image, modified_image, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text" + fps_col
        elif return_data == ReturnDataPhotosDB.all_modified_image:
            columns = "id, block_id, created_at, event_type, modified_image, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text" + fps_col
        elif return_data == ReturnDataPhotosDB.all_original_image:
            columns = "id, block_id, created_at, event_type, original_image, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text" + fps_col
        elif return_data == ReturnDataPhotosDB.all_except_photos:
            columns = "id, block_id, created_at, event_type, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text" + fps_col
        elif return_data == ReturnDataPhotosDB.only_ids:
            columns = "id"
        else:
            columns = "*"

        if 'deleted' in column_names and ignore_deleted == True:
            stmt = f"SELECT {columns} FROM events WHERE created_at BETWEEN '{date_start}' AND '{date_end}' AND deleted != 1"
        else:
            stmt = f"SELECT {columns} FROM events WHERE created_at BETWEEN '{date_start}' AND '{date_end}'"
        if mouse_only:
            stmt = f"{stmt} AND mouse_probability >= {mouse_probability}"
        if cats_only:
            stmt = f"{stmt} AND rfid != ''"
        # reverse the row order, based on column 'id', so that the newest events are at the top
        stmt = f"{stmt} ORDER BY id DESC"

        if elements_per_page != sys.maxsize:
            if offset is None:
                # Reverse paging: page_index 0 is the oldest page.
                total_rows = DatabaseCore.read_df_from_database(database, f"SELECT COUNT(*) as count FROM ({stmt})").iloc[0]['count']
                total_pages = (total_rows + elements_per_page - 1) // elements_per_page
                offset = (total_pages - page_index - 1) * elements_per_page
            stmt = f"{stmt} LIMIT {int(elements_per_page)} OFFSET {int(offset)}"

        logging.debug(f"[DATABASE] query EventsRepo.db_get_photos: return_data={return_data}, date_start={date_start}, date_end={date_end}, cats_only={cats_only}, mouse_only={mouse_only}, mouse_probability={mouse_probability}, page_index={page_index}, elements_per_page={elements_per_page}, ignore_deleted={ignore_deleted}")

        df = DatabaseCore.read_df_from_database(database, stmt)

        # Inject filesystem stored original images (backward compatibility)
        if not df.empty and 'original_image' in df.columns:
            for idx, row in df.iterrows():
                if row.get('original_image') is None:
                    img_path = EventsRepo._original_image_path(int(row['id'])) if 'id' in row else None
                    if img_path and os.path.exists(img_path):
                        try:
                            with open(img_path, 'rb') as f:
                                df.at[idx, 'original_image'] = f.read()
                        except Exception as e:
                            logging.warning(f"[DATABASE] Failed to read original image file '{img_path}': {e}")

        return df

    @staticmethod
    def db_get_photos_by_block_id(
        database: str,
        block_id: int,
        return_data: ReturnDataPhotosDB = ReturnDataPhotosDB.all,
        ignore_deleted: bool = True,
    ):
        """Return all event rows for one motion ``block_id`` (hydrates image files)."""
        columns_info = DatabaseCore.read_column_info_from_database(database, "events")
        column_names = set(info[1] for info in (columns_info or []))
        dims_cols = ", img_width, img_height" if ('img_width' in column_names and 'img_height' in column_names) else ""
        fps_col = ", effective_fps" if ('effective_fps' in column_names) else ""

        if return_data == ReturnDataPhotosDB.all:
            columns = "id, block_id, created_at, event_type, original_image, modified_image, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text" + dims_cols + fps_col + ", thumbnail"
        elif return_data == ReturnDataPhotosDB.all_modified_image:
            columns = "id, block_id, created_at, event_type, modified_image, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text" + dims_cols + fps_col + ", thumbnail"
        elif return_data == ReturnDataPhotosDB.all_original_image:
            columns = "id, block_id, created_at, event_type, original_image, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text" + dims_cols + fps_col + ", thumbnail"
        elif return_data == ReturnDataPhotosDB.all_except_photos:
            columns = "id, block_id, created_at, event_type, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text" + dims_cols + fps_col
        elif return_data == ReturnDataPhotosDB.only_ids:
            columns = "id"

        # Check if 'deleted' column exists (this column exists only in the kittyhack database)
        if 'deleted' in column_names and ignore_deleted is True:
            stmt = f"SELECT {columns} FROM events WHERE block_id = {block_id} AND deleted != 1"
        else:
            stmt = f"SELECT {columns} FROM events WHERE block_id = {block_id}"
        df = DatabaseCore.read_df_from_database(database, stmt)

        if not df.empty and 'original_image' in df.columns:
            for idx, row in df.iterrows():
                if row.get('original_image') is None:
                    img_path = EventsRepo._original_image_path(int(row['id'])) if 'id' in row else None
                    if img_path and os.path.exists(img_path):
                        try:
                            with open(img_path, 'rb') as f:
                                df.at[idx, 'original_image'] = f.read()
                        except Exception as e:
                            logging.warning(f"[DATABASE] Failed to read original image file '{img_path}': {e}")

        if not df.empty and 'thumbnail' in df.columns:
            for idx, row in df.iterrows():
                if row.get('thumbnail') is None:
                    thumb_path = EventsRepo._thumbnail_image_path(int(row['id'])) if 'id' in row else None
                    if thumb_path and os.path.exists(thumb_path):
                        try:
                            with open(thumb_path, 'rb') as f:
                                df.at[idx, 'thumbnail'] = f.read()
                        except Exception as e:
                            logging.warning(f"[DATABASE] Failed to read thumbnail file '{thumb_path}': {e}")
        return df

    @staticmethod
    def db_count_photos(
        database: str,
        date_start: str = "2020-01-01 00:00:00",
        date_end: str = "2100-12-31 23:59:59",
        cats_only: bool = False,
        mouse_only: bool = False,
        mouse_probability: float = 0.0,
        ignore_deleted: bool = True,
    ) -> int:
        """Count event rows matching the same filters as ``db_get_photos``."""
        try:
            where = f"created_at BETWEEN '{date_start}' AND '{date_end}'"

            # Check if 'deleted' column exists (this column exists only in the kittyhack database)
            columns_info = DatabaseCore.read_column_info_from_database(database, "events")
            column_names = [info[1] for info in columns_info]
            if 'deleted' in column_names and ignore_deleted:
                where += " AND deleted != 1"

            if mouse_only:
                where += f" AND mouse_probability >= {float(mouse_probability)}"
            if cats_only:
                where += " AND rfid != ''"

            stmt = f"SELECT COUNT(*) AS count FROM events WHERE {where}"
            df = DatabaseCore.read_df_from_database(database, stmt)
            if df.empty:
                return 0
            try:
                return int(df.iloc[0]['count'])
            except Exception:
                # sqlite can return tuple-like data depending on pandas version
                return int(df.iloc[0][0])
        except Exception as e:
            logging.error(f"[DATABASE] Failed counting photos: {e}")
            return 0

    @staticmethod
    def get_ids_without_thumbnail(database: str):
        """Return event IDs that still need a thumbnail file generated."""
        stmt = "SELECT id FROM events WHERE thumbnail IS NULL AND deleted != 1"
        df = DatabaseCore.read_df_from_database(database, stmt)
        if df.empty:
            return []
        result_ids = []
        for __, row in df.iterrows():
            img_id = int(row['id'])
            thumb_path = EventsRepo._thumbnail_image_path(img_id)
            # Only report IDs where both DB thumbnail is NULL and no file exists
            if not os.path.exists(thumb_path):
                result_ids.append(img_id)
        return result_ids

    @staticmethod
    def get_thubmnail_by_id(database: str, photo_id: int):
        """Load or create the thumbnail for ``photo_id`` (file-first, DB fallback)."""
        thumb_path = EventsRepo._thumbnail_image_path(photo_id)
        if os.path.exists(thumb_path):
            try:
                with open(thumb_path, 'rb') as f:
                    return f.read()
            except Exception as e:
                logging.warning(f"[DATABASE] Failed to read thumbnail file '{thumb_path}': {e}")

        # Legacy DB fallback / or need to create new thumbnail
        stmt = f"SELECT thumbnail, original_image FROM events WHERE id = {photo_id}"
        df = DatabaseCore.read_df_from_database(database, stmt)
        if df.empty:
            logging.error(f"[DATABASE] Photo with ID {photo_id} not found")
            # Return a placeholder thumbnail
            return EventsRepo._make_placeholder_image("Image not found", size=(320, 240))

        # If legacy thumbnail blob exists, write it to file (for migration) and return
        legacy_thumb = df.iloc[0]['thumbnail']
        if legacy_thumb is not None:
            try:
                with open(thumb_path, 'wb') as f:
                    f.write(legacy_thumb)
            except Exception as e:
                logging.warning(f"[DATABASE] Failed to persist legacy thumbnail for ID {photo_id}: {e}")
            return legacy_thumb

        # Determine original image source: file preferred, fallback to DB blob
        orig_path = EventsRepo._original_image_path(photo_id)
        original_image = None
        if os.path.exists(orig_path):
            try:
                with open(orig_path, 'rb') as f:
                    original_image = f.read()
            except Exception as e:
                logging.warning(f"[DATABASE] Failed to read original image file '{orig_path}': {e}")
        else:
            original_image = df.iloc[0]['original_image']

        if original_image is None:
            logging.error(f"[DATABASE] Original image not found for photo ID {photo_id}")
            # Provide a placeholder thumbnail and persist it
            thumbnail = EventsRepo._make_placeholder_image("Image not found", size=(320, 240))
            try:
                with open(thumb_path, 'wb') as f:
                    f.write(thumbnail)
            except Exception as e:
                logging.warning(f"[DATABASE] Failed to write placeholder thumbnail '{thumb_path}': {e}")
            return thumbnail

        try:
            thumbnail = ImageUtil.process_image(original_image, 640, 480, 50)
            # Persist thumbnail to filesystem (no longer stored as BLOB for new rows)
            try:
                with open(thumb_path, 'wb') as f:
                    f.write(thumbnail)
            except Exception as e:
                logging.warning(f"[DATABASE] Failed to write thumbnail file '{thumb_path}': {e}")
            return thumbnail
        except Exception as e:
            logging.error(f"[DATABASE] Failed to create thumbnail from original image: {e}")
            # Return a placeholder if processing fails
            return EventsRepo._make_placeholder_image("Image not found", size=(320, 240))

    @staticmethod
    def write_motion_timeline(database: str, block_id: int, timeline_entries: list) -> Result:
        """Persist the action timeline for a motion block."""
        if not timeline_entries:
            return Result(True, "")
        result = DatabaseCore.lock_database()
        if not result.success:
            return result
        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO motion_timeline (block_id, timeline_json) VALUES (?, ?)",
                (int(block_id), json.dumps(timeline_entries, ensure_ascii=False)),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            error_message = f"[DATABASE] Failed to write motion timeline for block {block_id}: {e}"
            logging.error(error_message)
            return Result(False, error_message)
        finally:
            DatabaseCore.release_database()
        return Result(True, "")

    @staticmethod
    def delete_motion_timeline_by_block_id(database: str, block_id: int) -> Result:
        """Delete the motion_timeline row for ``block_id``."""
        stmt = f"DELETE FROM motion_timeline WHERE block_id = {int(block_id)}"
        return DatabaseCore.write_stmt_to_database(database, stmt)

    @staticmethod
    def db_get_motion_timelines(database: str, block_ids: list[int]) -> dict:
        """Return {block_id: timeline_entries} for the given block IDs."""
        if not block_ids:
            return {}
        if not DatabaseCore.check_if_table_exists(database, "motion_timeline"):
            return {}
        ids = ",".join(str(int(b)) for b in block_ids)
        stmt = f"SELECT block_id, timeline_json FROM motion_timeline WHERE block_id IN ({ids})"
        df = DatabaseCore.read_df_from_database(database, stmt)
        result = {}
        if df is None or df.empty:
            return result
        for __, row in df.iterrows():
            try:
                entries = json.loads(row["timeline_json"] or "[]")
                result[int(row["block_id"])] = entries if isinstance(entries, list) else []
            except Exception:
                result[int(row["block_id"])] = []
        return result

    @staticmethod
    def read_photo_by_id(database: str, photo_id: int) -> pd.DataFrame:
        """Load one event row by ID, hydrating original image from disk if needed."""
        columns_info = DatabaseCore.read_column_info_from_database(database, "events")
        column_names = set(info[1] for info in (columns_info or []))
        fps_col = ", effective_fps" if ('effective_fps' in column_names) else ""
        columns = "id, block_id, created_at, event_type, original_image, modified_image, mouse_probability, no_mouse_probability, own_cat_probability, rfid, event_text" + fps_col
        stmt = f"SELECT {columns} FROM events WHERE id = {photo_id}"
        df = DatabaseCore.read_df_from_database(database, stmt)
        if not df.empty and 'original_image' in df.columns:
            row = df.iloc[0]
            if row.get('original_image') is None:
                img_path = EventsRepo._original_image_path(int(row['id']))
                if os.path.exists(img_path):
                    try:
                        with open(img_path, 'rb') as f:
                            df.at[0, 'original_image'] = f.read()
                    except Exception as e:
                        logging.warning(f"[DATABASE] Failed to read original image file '{img_path}': {e}")
                else:
                    # Provide a placeholder original image instead of None
                    logging.error(f"[DATABASE] Original image not found for photo ID {photo_id}")
                    df.at[0, 'original_image'] = EventsRepo._make_placeholder_image("Image not found", size=(640, 480))
        return df

    @staticmethod
    def delete_photo_by_id(database: str, photo_id: int) -> Result:
        """Soft-delete one event and remove its image/bundle files."""
        # Fetch block_id first so we can invalidate its bundle.
        block_id: int | None = None
        try:
            df_block = DatabaseCore.read_df_from_database(database, f"SELECT block_id FROM events WHERE id = {photo_id}")
            if not df_block.empty and 'block_id' in df_block.columns:
                block_id = int(df_block.iloc[0]['block_id'])
        except Exception as e:
            logging.debug(f"[DATABASE] Failed reading block_id for photo ID {photo_id}: {e}")

        stmt = f"UPDATE events SET original_image = NULL, modified_image = NULL, thumbnail = NULL, deleted = 1 WHERE id = {photo_id}"
        result = DatabaseCore.write_stmt_to_database(database, stmt)
        if result.success is True:
            removed_orig, removed_thumb = EventsRepo._remove_event_image_files([photo_id])
            removed_bundles = 0
            if block_id is not None:
                removed_bundles = EventsRepo._remove_event_bundle_files([block_id])
            logging.info(
                f"[DATABASE] Photo with ID '{photo_id}' deleted "
                f"(filesystem removed: originals={removed_orig}, thumbnails={removed_thumb}, bundles={removed_bundles})."
            )
        return result

    @staticmethod
    def delete_photos_by_block_id(database: str, block_id: int) -> Result:
        """Soft-delete all events in a block and remove their image/bundle files."""
        # Collect IDs in this block first
        df_ids = DatabaseCore.read_df_from_database(database, f"SELECT id FROM events WHERE block_id = {block_id}")
        ids = df_ids['id'].tolist() if not df_ids.empty else []

        stmt = f"UPDATE events SET original_image = NULL, modified_image = NULL, thumbnail = NULL, deleted = 1 WHERE block_id = {block_id}"
        result = DatabaseCore.write_stmt_to_database(database, stmt)
        if result.success is True:
            removed_orig, removed_thumb = EventsRepo._remove_event_image_files(ids)
            removed_bundles = EventsRepo._remove_event_bundle_files([block_id])
            logging.info(
                f"[DATABASE] Photos with block ID '{block_id}' deleted "
                f"(filesystem removed: originals={removed_orig}, thumbnails={removed_thumb}, bundles={removed_bundles})."
            )
            EventsRepo.delete_motion_timeline_by_block_id(database, block_id)
        return result

    @staticmethod
    def create_json_from_event(detected_objects: List[DetectedObject]) -> str:
        """Serialize detected objects into the events.event_text JSON shape."""
        event_data: EventSchema = {
            'detected_objects': [{
                'object_name': obj.object_name,
                'probability': round(float(obj.probability), 2),
                'x': round(float(obj.x), 3),
                'y': round(float(obj.y), 3),
                'width': round(float(obj.width), 3),
                'height': round(float(obj.height), 3)
            } for obj in detected_objects],
            'event_text': ''
        }
        return json.dumps(event_data)

    @staticmethod
    def read_event_from_json(event_json: str) -> List[DetectedObject]:
        """Parse events.event_text JSON back into DetectedObject instances."""
        try:
            event_data = json.loads(event_json)
            detected_objects = []
            for obj in event_data.get('detected_objects', []):
                detected_objects.append(DetectedObject(
                    object_name=obj['object_name'],
                    probability=float(obj['probability']),
                    x=float(obj['x']),
                    y=float(obj['y']),
                    width=float(obj['width']),
                    height=float(obj['height'])
                ))
            return detected_objects
        except Exception as e:
            logging.error(f"[DATABASE] Failed to parse event JSON: {e}")
            return []

    @staticmethod
    def get_detected_object_by_index(detected_objects: List[DetectedObject], index: int) -> DetectedObject:
        """Return ``detected_objects[index]``, or None if out of range."""
        if index < len(detected_objects):
            return detected_objects[index]
        return None

    @staticmethod
    def write_motion_block_to_db(
        database: str,
        buffer_block_id: int,
        event_type: str = "image",
        delete_from_buffer: bool = True,
        generate_thumbnails: bool = True,
        timeline_entries: list | None = None,
    ):
        """Persist an image-buffer motion block into ``events`` (+ optional timeline)."""
        result = DatabaseCore.lock_database()
        if not result.success:
            return result

        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()

            # Read the max value of the coumn 'block_id' and increment it
            cursor.execute("SELECT MAX(block_id) FROM events")
            db_block_id = cursor.fetchone()[0]
            if db_block_id is None:
                db_block_id = 0
            else:
                db_block_id += 1

            elements = image_buffer.get_by_block_id(buffer_block_id)
            logging.info(f"[DATABASE] Writing {len(elements)} images from buffer image block '{buffer_block_id}' as database block '{db_block_id}' to '{database}'.")

            # Determine whether optional columns exist (avoid nested lock acquisition).
            try:
                cursor.execute("PRAGMA table_info(events)")
                _cols = set(r[1] for r in cursor.fetchall())
                has_dims = ('img_width' in _cols and 'img_height' in _cols)
                has_effective_fps = ('effective_fps' in _cols)
            except Exception:
                has_dims = False
                has_effective_fps = False

            # Check for motion_timeline table using the open cursor (avoids re-acquiring the lock).
            try:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='motion_timeline'")
                has_motion_timeline = bool(cursor.fetchone())
            except Exception:
                has_motion_timeline = False

            # Decide the max number of pictures to write to the database, based on the content of the first element.tag_id
            # (every element of the block has the same tag_id)
            max_images = CONFIG['MAX_PICTURES_PER_EVENT_WITH_RFID'] if elements[0].tag_id else CONFIG['MAX_PICTURES_PER_EVENT_WITHOUT_RFID']

            # Compute effective FPS for this event block based on element timestamps.
            # This should match the real spacing of the stored frames (target devices often ~3fps,
            # remote-mode may be higher depending on REMOTE_INFERENCE_MAX_FPS).
            effective_fps_block = None
            try:
                el_for_fps = list(elements[:max_images])
                if len(el_for_fps) >= 2:
                    t0 = float(el_for_fps[0].timestamp)
                    t1 = float(el_for_fps[-1].timestamp)
                    span = float(t1 - t0)
                    if span > 0:
                        effective_fps_block = float(len(el_for_fps) - 1) / span
            except Exception:
                effective_fps_block = None

            if effective_fps_block is None:
                try:
                    effective_fps_block = float(CONFIG.get('REMOTE_INFERENCE_MAX_FPS', 10.0) or 10.0)
                except Exception:
                    effective_fps_block = 10.0

            try:
                effective_fps_block = float(effective_fps_block)
            except Exception:
                effective_fps_block = 10.0
            if effective_fps_block < 0.1:
                effective_fps_block = 0.1
            if effective_fps_block > 60.0:
                effective_fps_block = 60.0

            index = 0
            for element in elements:
                # Write the image to the database, if the index is less than the maximum number of images
                if index < max_images:
                    try:
                        detected_objects = element.detected_objects if element.detected_objects is not None else []
                        event_json = EventsRepo.create_json_from_event(detected_objects)
                    except Exception as e:
                        event_json = json.dumps({'detected_objects': [], 'event_text': ''})
                        logging.error(f"[DATABASE] Failed to serialize event data: {e}")

                    img_w = None
                    img_h = None
                    if has_dims and element.original_image is not None:
                        try:
                            s = EventsRepo.get_jpeg_size(element.original_image)
                            if s:
                                img_w, img_h = int(s[0]), int(s[1])
                        except Exception:
                            pass

                    columns = "block_id, created_at, event_type, original_image, modified_image, mouse_probability, no_mouse_probability, own_cat_probability, rfid, event_text"
                    if has_dims:
                        columns += ", img_width, img_height"
                    if has_effective_fps:
                        columns += ", effective_fps"
                    values = ', '.join(['?' for _ in columns.split(', ')])
                    values_list = [
                        db_block_id,
                        DateTimeUtil.get_utc_date_string(element.timestamp),
                        event_type,
                        None,  # original_image now stored on filesystem
                        None if element.modified_image is None else element.modified_image,
                        element.mouse_probability,
                        element.no_mouse_probability,
                        element.own_cat_probability,
                        element.tag_id,
                        event_json
                    ]
                    if has_dims:
                        values_list.extend([img_w, img_h])
                    if has_effective_fps:
                        values_list.append(float(effective_fps_block))
                    cursor.execute(f"INSERT INTO events ({columns}) VALUES ({values})", values_list)
                    new_row_id = cursor.lastrowid
                    # Persist original image to filesystem (if available)
                    if element.original_image is not None:
                        orig_path = EventsRepo._original_image_path(new_row_id)
                        try:
                            with open(orig_path, 'wb') as f:
                                f.write(element.original_image)
                        except Exception as e:
                            logging.warning(f"[DATABASE] Failed to write original image file '{orig_path}': {e}")
                    index += 1

                # Delete the image from the buffer
                if delete_from_buffer:
                    image_buffer.delete_by_id(element.id)

            logging.info(f"[DATABASE] Wrote {index}/{len(elements)} images to the database (Limit per event: {max_images}).")

            # Check if the number of photos exceeds the maximum allowed number
            if 'MAX_PHOTOS_COUNT' in CONFIG:
                try:
                    max_count = int(CONFIG['MAX_PHOTOS_COUNT'])
                except Exception:
                    max_count = 0
                if max_count > 0:
                    EventsRepo._purge_excess_photos_with_cursor(
                        cursor, max_count, has_motion_timeline=has_motion_timeline
                    )

            if timeline_entries and has_motion_timeline:
                try:
                    cursor.execute(
                        "INSERT OR REPLACE INTO motion_timeline (block_id, timeline_json) VALUES (?, ?)",
                        (int(db_block_id), json.dumps(timeline_entries, ensure_ascii=False)),
                    )
                except Exception as e:
                    logging.warning(f"[DATABASE] Could not store motion timeline for block {db_block_id}: {e}")

            conn.commit()
            conn.close()
            # Update the timestamp of the last added image block
            last_imgblock_ts.update_timestamp(tm.time())
        except Exception as e:
            error_message = f"[DATABASE] An error occurred while writing images to the database '{database}': {e}"
            logging.error(error_message)
        else:
            logging.info(f"[DATABASE] Successfully wrote images to the database '{database}'.")
        finally:
            DatabaseCore.release_database()

        if generate_thumbnails:
            db_photo_ids = EventsRepo.db_get_photos_by_block_id(database, db_block_id, ReturnDataPhotosDB.only_ids)
            for db_photo_id in db_photo_ids['id']:
                EventsRepo.get_thubmnail_by_id(database, db_photo_id)
            logging.info(f"[DATABASE] Generated {len(db_photo_ids)} thumbnails for block ID '{db_block_id}'. (Filesystem storage)")

    @staticmethod
    def _purge_excess_photos_with_cursor(
        cursor, max_count: int, *, has_motion_timeline: bool | None = None
    ) -> int:
        """Soft-delete oldest active events until count <= max_count. Returns purged count."""
        if max_count <= 0:
            return 0

        cursor.execute("SELECT COUNT(*) FROM events WHERE deleted != 1")
        total_photos = int(cursor.fetchone()[0] or 0)
        if total_photos <= max_count:
            return 0

        excess_photos = total_photos - max_count
        logging.info(
            f"[DATABASE] Number of photos exceeds limit. Deleting {excess_photos} oldest photos."
        )
        cursor.execute(
            "SELECT id, created_at, block_id FROM events "
            "WHERE deleted != 1 ORDER BY created_at ASC LIMIT ?",
            (excess_photos,),
        )
        photos_to_delete = cursor.fetchall()
        ids_to_purge: List[int] = []
        blocks_to_invalidate: List[int] = []
        for photo in photos_to_delete:
            photo_id = photo[0]
            logging.debug(
                f"[DATABASE] Deleting photo ID: {photo_id}, created_at: {photo[1]}"
            )
            try:
                blocks_to_invalidate.append(int(photo[2]))
            except Exception:
                pass
            cursor.execute(
                "UPDATE events SET deleted = 1, original_image = NULL, "
                "modified_image = NULL, thumbnail = NULL WHERE id = ?",
                (photo_id,),
            )
            ids_to_purge.append(photo_id)

        if not ids_to_purge:
            return 0

        removed_orig, removed_thumb = EventsRepo._remove_event_image_files(ids_to_purge)
        removed_bundles = EventsRepo._remove_event_bundle_files(blocks_to_invalidate)
        logging.info(
            f"[DATABASE] Purged oldest photos "
            f"(filesystem removed: originals={removed_orig}, thumbnails={removed_thumb}, "
            f"bundles={removed_bundles})."
        )

        if has_motion_timeline is None:
            try:
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='motion_timeline'"
                )
                has_motion_timeline = bool(cursor.fetchone())
            except Exception:
                has_motion_timeline = False

        if blocks_to_invalidate and has_motion_timeline:
            unique_blocks = sorted({int(b) for b in blocks_to_invalidate})
            placeholders = ",".join("?" for _ in unique_blocks)
            cursor.execute(
                f"DELETE FROM motion_timeline WHERE block_id IN ({placeholders})",
                unique_blocks,
            )

        return len(ids_to_purge)

    @staticmethod
    def purge_excess_photos(
        database: str, max_count: int | None = None
    ) -> Result:
        """Enforce ``MAX_PHOTOS_COUNT`` immediately; message is the purged count."""
        if max_count is None:
            try:
                max_count = int(CONFIG.get("MAX_PHOTOS_COUNT") or 0)
            except Exception:
                max_count = 0
        if max_count <= 0:
            return Result(True, "0")

        result = DatabaseCore.lock_database()
        if not result.success:
            return result

        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            purged = EventsRepo._purge_excess_photos_with_cursor(cursor, int(max_count))
            conn.commit()
            conn.close()
            return Result(True, str(purged))
        except Exception as e:
            error_message = f"[DATABASE] Failed to purge excess photos: {e}"
            logging.error(error_message)
            return Result(False, error_message)
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def cleanup_orphan_image_files(database: str) -> Result:
        """Delete original/thumbnail/bundle files with no matching active event."""
        try:
            # Collect valid IDs from DB (include non-deleted only)
            result = DatabaseCore.lock_database()
            if not result.success:
                return result
            try:
                conn = sqlite3.connect(database, timeout=30)
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM events WHERE deleted != 1")
                valid_ids = set(int(r[0]) for r in cursor.fetchall())
                cursor.execute("SELECT DISTINCT block_id FROM events WHERE deleted != 1")
                valid_block_ids = set(int(r[0]) for r in cursor.fetchall() if r and r[0] is not None)
                conn.close()
            except Exception as e:
                logging.error(f"[ORPHAN_CLEANUP] Failed to read event IDs: {e}")
                return Result(False, "read_ids_failed")
            finally:
                DatabaseCore.release_database()

            def collect_orphans(dir_path: str) -> List[str]:
                orphans = []
                try:
                    for name in os.listdir(dir_path):
                        if not name.lower().endswith(".jpg"):
                            continue
                        stem = name[:-4]
                        if stem.isdigit():
                            fid = int(stem)
                            if fid not in valid_ids:
                                orphans.append(os.path.join(dir_path, name))
                        else:
                            # Any non-numeric *.jpg in these dedicated dirs is considered orphan
                            orphans.append(os.path.join(dir_path, name))
                    return orphans
                except Exception as e:
                    logging.warning(f"[ORPHAN_CLEANUP] Failed to list '{dir_path}': {e}")
                    return []

            orphan_originals = collect_orphans(ORIGINAL_IMAGE_DIR)
            orphan_thumbs = collect_orphans(THUMBNAIL_DIR)

            removed = 0
            for path in orphan_originals + orphan_thumbs:
                try:
                    os.remove(path)
                    removed += 1
                except Exception as e:
                    logging.warning(f"[ORPHAN_CLEANUP] Failed removing '{path}': {e}")

            # Also remove orphan event bundles for blocks that no longer exist.
            removed_bundles = 0
            try:
                for name in os.listdir(EVENT_BUNDLE_DIR):
                    if not (name.endswith('.tar') or name.endswith('.tar.gz')):
                        continue
                    m = None
                    try:
                        import re
                        m = re.match(r"^event_(\d+)\.tar(?:\.gz)?$", name)
                    except Exception:
                        m = None
                    if not m:
                        continue
                    bid = int(m.group(1))
                    if bid in valid_block_ids:
                        continue
                    try:
                        os.remove(os.path.join(EVENT_BUNDLE_DIR, name))
                        removed_bundles += 1
                    except Exception as e:
                        logging.warning(f"[ORPHAN_CLEANUP] Failed removing bundle '{name}': {e}")
            except Exception as e:
                logging.warning(f"[ORPHAN_CLEANUP] Failed bundle cleanup in '{EVENT_BUNDLE_DIR}': {e}")

            logging.info(
                f"[ORPHAN_CLEANUP] Removed {removed} orphan image files "
                f"(originals: {len(orphan_originals)}, thumbnails: {len(orphan_thumbs)}) "
                f"and {removed_bundles} orphan bundle files."
            )
            return Result(True, "")
        except Exception as e:
            logging.error(f"[ORPHAN_CLEANUP] Unexpected error: {e}")
            return Result(False, "unexpected_error")

    @staticmethod
    def db_get_motion_blocks(database: str, block_count: int = 0, date_start="2020-01-01 00:00:00", date_end="2100-12-31 23:59:59", cats_only=False, mouse_only=False, mouse_probability=0.0):
        """Return distinct motion blocks matching filters (newest first)."""
        columns = "block_id, created_at, event_type, rfid, event_text"
        where_clauses = ["deleted != 1", f"created_at BETWEEN '{date_start}' AND '{date_end}'"]

        if cats_only:
            where_clauses.append("rfid != ''")
        if mouse_only:
            where_clauses.append(f"mouse_probability >= {mouse_probability}")

        where_clause = " AND ".join(where_clauses)

        if block_count > 0:
            stmt = f"""
                SELECT {columns} FROM events 
                WHERE {where_clause}
                GROUP BY block_id 
                ORDER BY block_id DESC 
                LIMIT {block_count}
            """
        else:
            stmt = f"""
                SELECT {columns} FROM events 
                WHERE {where_clause}
                GROUP BY block_id 
                ORDER BY block_id DESC
            """
        return DatabaseCore.read_df_from_database(database, stmt)

    @staticmethod
    def cleanup_deleted_events(database: str) -> Result:
        """Hard-delete soft-deleted events older than the oldest active event."""
        result = DatabaseCore.lock_database()
        if not result.success:
            return result

        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()

            # Find the ID of the oldest non-deleted event
            cursor.execute("SELECT MIN(id) FROM events WHERE deleted != 1")
            min_active_id = cursor.fetchone()[0]

            if min_active_id is not None:
                # Delete all events older than the oldest non-deleted event
                cursor.execute("DELETE FROM events WHERE id < ? AND deleted = 1", (min_active_id,))
                deleted_count = cursor.rowcount
                conn.commit()

                if deleted_count > 0:
                    logging.info(f"[DATABASE] Cleaned up {deleted_count} deleted events from database.")
                else:
                    logging.info("[DATABASE] No deleted events found in database. Cleanup skipped.")
            else:
                logging.info("[DATABASE] No non-deleted events found in database. Cleanup skipped.")
                return Result(True, "")

            conn.close()
            return Result(True, "")

        except Exception as e:
            error_message = f"[DATABASE] Failed to clean up deleted events: {e}"
            logging.error(error_message)
            return Result(False, error_message)
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def get_ids_with_original_blob(database: str, include_deleted: bool = False) -> List[int]:
        """Return event IDs that still have a legacy original_image BLOB."""
        where = "original_image IS NOT NULL"
        if not include_deleted and DatabaseCore.check_if_column_exists(database, "events", "deleted"):
            where += " AND deleted != 1"

        result = DatabaseCore.lock_database()
        if not result.success:
            logging.error(f"[DATABASE] Failed to lock DB for EventsRepo.get_ids_with_original_blob: {result.message}")
            return []

        ids: List[int] = []
        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute(f"SELECT id FROM events WHERE {where} ORDER BY id")
            # Stream rows to avoid large memory spikes on huge tables
            fetch_size = 10000
            while True:
                rows = cursor.fetchmany(fetch_size)
                if not rows:
                    break
                ids.extend(int(r[0]) for r in rows)
            conn.close()
        except Exception as e:
            logging.error(f"[DATABASE] Failed to fetch IDs with original blobs: {e}")
        finally:
            DatabaseCore.release_database()
        return ids

    @staticmethod
    def perform_event_image_migration_ids(database: str,
                                          ids: List[int],
                                          progress_fn=None,
                                          chunk_size: int = 1000,
                                          batch_size: int = 200) -> Result:
        """Migrate listed event IDs' original/thumbnail BLOBs onto the filesystem."""
        start = tm.time()
        if not ids:
            return Result(True, "no_ids_provided")

        # Deduplicate & sort for predictable processing order
        target_ids = sorted(set(int(i) for i in ids if isinstance(i, (int, str))))
        logging.info(f"[MIGRATION_IDS] Starting targeted migration for {len(target_ids)} IDs...")

        migrated_original = 0
        migrated_thumbnail = 0
        missing_ids = 0
        skipped_existing_file_original = 0
        skipped_existing_file_thumbnail = 0

        # 1) Pre-scan blobs without holding the global lock to avoid blocking writers
        rows_cache: dict[int, tuple[bytes | None, bytes | None]] = {}
        total_units = 0
        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            for i in range(0, len(target_ids), chunk_size):
                chunk = target_ids[i:i+chunk_size]
                if not chunk:
                    continue
                placeholders = ','.join('?' for _ in chunk)
                cursor.execute(f"SELECT id, original_image, thumbnail FROM events WHERE id IN ({placeholders})", chunk)
                for rid, orig_blob, thumb_blob in cursor.fetchall():
                    rows_cache[int(rid)] = (orig_blob, thumb_blob)
                    if orig_blob is not None:
                        total_units += 1
                    if thumb_blob is not None:
                        total_units += 1
            conn.close()
        except Exception as e:
            logging.error(f"[MIGRATION_IDS] Pre-scan failed: {e}")
            return Result(False, "prescan_failed")

        missing_set = set(target_ids) - set(rows_cache.keys())
        missing_ids = len(missing_set)
        if missing_ids:
            logging.info(f"[MIGRATION_IDS] {missing_ids} IDs not found and will be skipped.")

        processed_units = 0

        # Pending updates to be applied under short locks
        pending_null_original: list[int] = []
        pending_null_thumbnail: list[int] = []

        def flush_updates():
            """Apply batched NULL-blob updates under a short lock."""
            nonlocal pending_null_original, pending_null_thumbnail
            if not pending_null_original and not pending_null_thumbnail:
                return
            lock_res = DatabaseCore.lock_database()
            if not lock_res.success:
                logging.error(f"[MIGRATION_IDS] Failed to acquire DB lock for batch update: {lock_res.message}")
                return
            try:
                conn_u = sqlite3.connect(database, timeout=30)
                cur_u = conn_u.cursor()
                if pending_null_original:
                    cur_u.executemany("UPDATE events SET original_image = NULL WHERE id = ?", [(rid,) for rid in pending_null_original])
                if pending_null_thumbnail:
                    cur_u.executemany("UPDATE events SET thumbnail = NULL WHERE id = ?", [(rid,) for rid in pending_null_thumbnail])
                conn_u.commit()
                conn_u.close()
            except Exception as e:
                logging.error(f"[MIGRATION_IDS] Batch NULL update failed: {e}")
            finally:
                DatabaseCore.release_database()
                pending_null_original.clear()
                pending_null_thumbnail.clear()

        # 2) Migration pass: write files unlocked, queue DB updates
        for rid in target_ids:
            if rid not in rows_cache:
                continue
            orig_blob, thumb_blob = rows_cache[rid]

            # Original image -> filesystem
            if orig_blob is not None:
                orig_path = EventsRepo._original_image_path(rid)
                if not os.path.exists(orig_path):
                    try:
                        with open(orig_path, 'wb') as f:
                            f.write(orig_blob)
                        migrated_original += 1
                    except Exception as e:
                        logging.warning(f"[MIGRATION_IDS] Failed writing original image for id={rid}: {e}")
                        # keep blob, skip nulling
                    else:
                        pending_null_original.append(rid)
                else:
                    pending_null_original.append(rid)
                    skipped_existing_file_original += 1
                processed_units += 1
                if progress_fn:
                    try:
                        progress_fn(processed_units, total_units)
                    except Exception:
                        pass

            # Thumbnail -> filesystem
            if thumb_blob is not None:
                thumb_path = EventsRepo._thumbnail_image_path(rid)
                if not os.path.exists(thumb_path):
                    try:
                        with open(thumb_path, 'wb') as f:
                            f.write(thumb_blob)
                        migrated_thumbnail += 1
                    except Exception as e:
                        logging.warning(f"[MIGRATION_IDS] Failed writing thumbnail for id={rid}: {e}")
                    else:
                        pending_null_thumbnail.append(rid)
                else:
                    pending_null_thumbnail.append(rid)
                    skipped_existing_file_thumbnail += 1
                processed_units += 1
                if progress_fn:
                    try:
                        progress_fn(processed_units, total_units)
                    except Exception:
                        pass

            # Flush when batch is full to minimize lock time and memory
            if len(pending_null_original) + len(pending_null_thumbnail) >= batch_size:
                flush_updates()

        # Final flush for remaining updates
        flush_updates()

        duration = round(tm.time() - start, 2)
        msg = (f"migrated_original={migrated_original}, migrated_thumbnail={migrated_thumbnail}, "
               f"skipped_existing_file_original={skipped_existing_file_original}, "
               f"skipped_existing_file_thumbnail={skipped_existing_file_thumbnail}, "
               f"missing_ids={missing_ids}, total_units={total_units}, duration_sec={duration}")
        logging.info(f"[MIGRATION_IDS] Finished targeted migration: {msg}")
        return Result(True, msg)


class CatsRepo:
    """Cats CRUD, RFID tags, settings map, and cat thumbnails."""

    @staticmethod
    def db_get_cats(database: str, return_data: ReturnDataCatDB):
        """Return all cats as a DataFrame (optionally without image blobs)."""
        if return_data == ReturnDataCatDB.all:
             columns = "*"
        elif return_data == ReturnDataCatDB.all_except_photos:
            columns = "id, created_at, name, rfid, enable_prey_detection, allow_entry, allow_exit"

        stmt = f"SELECT {columns} FROM cats"
        return DatabaseCore.read_df_from_database(database, stmt)

    @staticmethod
    def db_get_all_rfid_tags(database: str):
        """Return RFID tags for all cats (falls back to lowercase name if empty)."""
        stmt = "SELECT rfid, name FROM cats"
        df = DatabaseCore.read_df_from_database(database, stmt)
        if df.empty:
            return []

        result = []
        for __, row in df.iterrows():
            rfid = row['rfid']
            # Use name (lowercase) as fallback if rfid is empty
            if not rfid:
                rfid = row['name'].lower()
            result.append(rfid)

        return result

    @staticmethod
    def db_delete_cat_by_id(database: str, cat_id: int) -> Result:
        """Delete a cat row by ID."""
        stmt = f"DELETE FROM cats WHERE id = {cat_id}"
        result = DatabaseCore.write_stmt_to_database(database, stmt)
        if result.success == True:
            logging.info(f"[DATABASE] Cat with ID '{cat_id}' deleted successfully.")
        return result

    @staticmethod
    def db_update_cat_data_by_id(database: str, cat_id: int, name: str, rfid: str, cat_image_path: str, enable_prey_detection: bool = True, allow_entry: bool = True, allow_exit: bool = True) -> Result:
        """Update cat fields by ID; pass ``cat_image_path=None`` to keep the image."""
        if cat_image_path:
            try:
                img = cv2.imread(cat_image_path)
                cat_image_blob = ImageUtil.resize_image_to_square(img, 800, 85)
            except Exception as e:
                error_message = f"[DATABASE] Failed to read image file '{cat_image_path}': {e}"
                logging.error(error_message)
                return Result(False, error_message)
        else:
            cat_image_blob = None

        result = DatabaseCore.lock_database()
        if not result.success:
            return result

        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            if cat_image_blob is None:
                cursor.execute(
                    "UPDATE cats SET name = ?, rfid = ?, enable_prey_detection = ?, allow_entry = ?, allow_exit = ? WHERE id = ?",
                    (name, rfid, int(enable_prey_detection), int(allow_entry), int(allow_exit), cat_id)
                )
            else:
                cursor.execute(
                    "UPDATE cats SET name = ?, rfid = ?, cat_image = ?, enable_prey_detection = ?, allow_entry = ?, allow_exit = ? WHERE id = ?",
                    (name, rfid, cat_image_blob, int(enable_prey_detection), int(allow_entry), int(allow_exit), cat_id)
                )
            conn.commit()
            conn.close()
        except Exception as e:
            error_message = f"[DATABASE] An error occurred while updating the cat data in the database '{database}': {e}"
            logging.error(error_message)
            return Result(False, error_message)
        else:
            logging.info(f"[DATABASE] Cat data with ID '{cat_id}' updated successfully.")
            return Result(True, "")
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def db_add_new_cat(database: str, name: str, rfid: str, cat_image_path: str,
                       enable_prey_detection: bool = True, allow_entry: bool = True, allow_exit: bool = True) -> Result: 
        """Insert a new cat row (optional JPEG path becomes the cat_image blob)."""
        if cat_image_path:
            try:
                with open(cat_image_path, 'rb') as file:
                    cat_image_blob = file.read()
            except Exception as e:
                error_message = f"[DATABASE] Failed to read image file '{cat_image_path}': {e}"
                logging.error(error_message)
                return Result(False, error_message)
        else:
            cat_image_blob = None

        result = DatabaseCore.lock_database()
        if not result.success:
            return result

        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(id) FROM cats")
            id = cursor.fetchone()[0]
            if id is None:
                id = 0
            else:
                id += 1
            cursor.execute(
                "INSERT INTO cats (id, created_at, name, rfid, cat_image, enable_prey_detection, allow_entry, allow_exit) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (id, DateTimeUtil.get_utc_date_string(tm.time()), name, rfid, cat_image_blob, int(enable_prey_detection), int(allow_entry), int(allow_exit))
            )
            conn.commit()
            conn.close()
        except Exception as e:
            error_message = f"[DATABASE] An error occurred while adding a new cat to the database '{database}': {e}"
            logging.error(error_message)
            return Result(False, error_message)
        else:
            logging.info(f"[DATABASE] New cat added successfully to the database '{database}' with ID '{id}'.")
            return Result(True, "")
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def get_cat_settings_map(database: str) -> dict:
        """Map RFID (or lowercase name) → per-cat prey/entry/exit settings."""
        try:
            df = DatabaseCore.read_df_from_database(database, "SELECT name, rfid, enable_prey_detection, allow_entry, allow_exit FROM cats")
            settings = {}
            if df.empty:
                return settings
            for __, row in df.iterrows():
                key = row['rfid'] if row['rfid'] else str(row['name']).lower()
                epd = row['enable_prey_detection'] if 'enable_prey_detection' in row and pd.notna(row['enable_prey_detection']) else 1
                ae = row['allow_entry'] if 'allow_entry' in row and pd.notna(row['allow_entry']) else 1
                ax = row['allow_exit'] if 'allow_exit' in row and pd.notna(row['allow_exit']) else 1
                settings[key] = {
                    'enable_prey_detection': bool(int(epd)),
                    'allow_entry': bool(int(ae)),
                    'allow_exit': bool(int(ax)),
                }
            return settings
        except Exception as e:
            logging.error(f"[DATABASE] Failed to build cat settings map: {e}")
            return {}

    @staticmethod
    def get_cat_name_rfid_dict(database: str):
        """Map RFID (or lowercase name) → display name."""
        stmt = "SELECT rfid, name FROM cats"
        df_cats = DatabaseCore.read_df_from_database(database, stmt)

        # Create dictionary with fallback logic
        result = {}
        for __, row in df_cats.iterrows():
            rfid = row['rfid']
            name = row['name']
            # Use name (lowercase) as fallback if rfid is empty
            if not rfid:
                rfid = name.lower()
            result[rfid] = name

        return result

    @staticmethod
    def get_cat_names_list(database: str):
        """Return a list of all cat names."""
        stmt = "SELECT name FROM cats"
        df_cats = DatabaseCore.read_df_from_database(database, stmt)
        return df_cats['name'].tolist() if not df_cats.empty else []

    @staticmethod
    def get_cat_thumbnail(database_path, cat_id, size=(32, 32)):
        """Return a cached base64 JPEG thumbnail for ``cat_id``, or None."""
        cache_key = (cat_id, size)
        if cache_key in _cat_thumbnail_cache:
            return _cat_thumbnail_cache[cache_key]
        try:
            # Fetch the cat image from the database
            df = CatsRepo.db_get_cats(database_path, ReturnDataCatDB.all)
            row = df[df['id'] == cat_id]
            if row.empty or row.iloc[0]['cat_image'] is None:
                return None
            img_bytes = row.iloc[0]['cat_image']
            img_array = np.frombuffer(img_bytes, np.uint8)
            img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
            if img is None:
                return None
            # Resize to thumbnail
            thumb = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
            # Encode as JPEG
            ret, buf = cv2.imencode('.jpg', thumb)
            if not ret:
                return None
            b64_thumb = base64.b64encode(buf.tobytes()).decode('utf-8')
            _cat_thumbnail_cache[cache_key] = b64_thumb
            return b64_thumb
        except Exception as e:
            logging.error(f"Failed to create cat thumbnail for cat_id={cat_id}: {e}")
            return None


class VisitStatsRepo:
    """Durable per-visit statistics (independent of picture purge)."""

    _INSERT_SQL = """
        INSERT INTO visit_stats (
            created_at, cat_rfid, cat_name, conclusion, direction,
            passed, prey, denied_entry, denied_exit, attempt_no_pass,
            uncertain_entry, duration_outside_s, duration_inside_s, source_block_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    @staticmethod
    def insert(database: str, row: dict) -> Result:
        """Insert one visit_stats row. Duplicate ``source_block_id`` is ignored."""
        result = DatabaseCore.lock_database()
        if not result.success:
            return result
        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute(
                VisitStatsRepo._INSERT_SQL,
                (
                    row.get("created_at"),
                    row.get("cat_rfid"),
                    row.get("cat_name"),
                    row.get("conclusion"),
                    row.get("direction"),
                    int(row.get("passed") or 0),
                    int(row.get("prey") or 0),
                    int(row.get("denied_entry") or 0),
                    int(row.get("denied_exit") or 0),
                    int(row.get("attempt_no_pass") or 0),
                    int(row.get("uncertain_entry") or 0),
                    row.get("duration_outside_s"),
                    row.get("duration_inside_s"),
                    row.get("source_block_id"),
                ),
            )
            conn.commit()
            conn.close()
        except sqlite3.IntegrityError:
            logging.debug(
                "[VISIT_STATS] Skipping duplicate source_block_id=%s",
                row.get("source_block_id"),
            )
            return Result(True, "duplicate")
        except Exception as e:
            error_message = f"[VISIT_STATS] Failed to insert visit: {e}"
            logging.error(error_message)
            return Result(False, error_message)
        else:
            return Result(True, "")
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def get_last_passed(database: str, cat_rfid: str) -> dict | None:
        """Latest passed in/out row for ``cat_rfid``, or None."""
        if not cat_rfid:
            return None
        result = DatabaseCore.lock_database()
        if not result.success:
            return None
        try:
            conn = sqlite3.connect(database, timeout=30)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, created_at, direction, conclusion
                FROM visit_stats
                WHERE cat_rfid = ? AND passed = 1 AND direction IN ('in', 'out')
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (cat_rfid,),
            )
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception as e:
            logging.error(f"[VISIT_STATS] Failed to read last passed visit: {e}")
            return None
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def query_range(
        database: str,
        date_start: str,
        date_end: str,
        cat_rfid: str | None = None,
    ) -> list[dict]:
        """Return visit_stats rows in ``[date_start, date_end]``, oldest first."""
        if not DatabaseCore.check_if_table_exists(database, "visit_stats"):
            return []
        result = DatabaseCore.lock_database()
        if not result.success:
            return []
        try:
            conn = sqlite3.connect(database, timeout=30)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            sql = """
                SELECT * FROM visit_stats
                WHERE created_at >= ? AND created_at <= ?
            """
            params: list = [date_start, date_end]
            if cat_rfid == "__unknown__":
                sql += " AND (cat_rfid IS NULL OR cat_rfid = '')"
            elif cat_rfid:
                sql += " AND cat_rfid = ?"
                params.append(cat_rfid)
            sql += " ORDER BY created_at ASC, id ASC"
            cursor.execute(sql, params)
            rows = [dict(r) for r in cursor.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logging.error(f"[VISIT_STATS] Failed to query range: {e}")
            return []
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def count(database: str) -> int:
        """Total visit_stats rows, or 0 if the table is missing."""
        if not DatabaseCore.check_if_table_exists(database, "visit_stats"):
            return 0
        df = DatabaseCore.read_df_from_database(
            database, "SELECT COUNT(*) AS n FROM visit_stats"
        )
        if df.empty:
            return 0
        try:
            return int(df.iloc[0]["n"])
        except Exception:
            return 0

    @staticmethod
    def list_cats(database: str) -> list[dict]:
        """Distinct RFID/name pairs seen in visit_stats (unknown = empty rfid)."""
        if not DatabaseCore.check_if_table_exists(database, "visit_stats"):
            return []
        df = DatabaseCore.read_df_from_database(
            database,
            "SELECT cat_rfid, cat_name FROM visit_stats GROUP BY cat_rfid, cat_name",
        )
        if df.empty:
            return []
        out = []
        for __, row in df.iterrows():
            out.append(
                {
                    "cat_rfid": row["cat_rfid"] if pd.notna(row["cat_rfid"]) else None,
                    "cat_name": row["cat_name"] if pd.notna(row["cat_name"]) else None,
                }
            )
        return out

    @staticmethod
    def get_meta(database: str, key: str) -> str | None:
        """Read a visit_stats_meta value, or None."""
        if not DatabaseCore.check_if_table_exists(database, "visit_stats_meta"):
            return None
        result = DatabaseCore.lock_database()
        if not result.success:
            return None
        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM visit_stats_meta WHERE key = ?", (key,))
            row = cursor.fetchone()
            conn.close()
            return str(row[0]) if row else None
        except Exception as e:
            logging.error(f"[VISIT_STATS] Failed to read meta '{key}': {e}")
            return None
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def set_meta(database: str, key: str, value: str) -> Result:
        """Upsert a visit_stats_meta key."""
        result = DatabaseCore.lock_database()
        if not result.success:
            return result
        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO visit_stats_meta (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            error_message = f"[VISIT_STATS] Failed to set meta '{key}': {e}"
            logging.error(error_message)
            return Result(False, error_message)
        else:
            return Result(True, "")
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def fetch_motion_blocks_for_backfill(database: str) -> list[dict]:
        """One representative events-row per motion block, oldest first (includes deleted)."""
        if not DatabaseCore.check_if_table_exists(database, "events"):
            return []
        stmt = """
            SELECT e.block_id, e.created_at, e.event_type,
                   COALESCE(
                       NULLIF(TRIM(e.rfid), ''),
                       (
                           SELECT TRIM(e2.rfid) FROM events e2
                           WHERE e2.block_id = e.block_id
                             AND e2.rfid IS NOT NULL
                             AND TRIM(e2.rfid) != ''
                           LIMIT 1
                       )
                   ) AS rfid
            FROM events e
            INNER JOIN (
                SELECT block_id, MIN(id) AS min_id
                FROM events
                WHERE block_id IS NOT NULL
                GROUP BY block_id
            ) t ON e.id = t.min_id
            ORDER BY e.created_at ASC, e.id ASC
        """
        df = DatabaseCore.read_df_from_database(database, stmt)
        if df.empty:
            return []
        rows = []
        for __, row in df.iterrows():
            rfid = row["rfid"] if "rfid" in row and pd.notna(row["rfid"]) else None
            if isinstance(rfid, str) and not rfid.strip():
                rfid = None
            rows.append(
                {
                    "block_id": int(row["block_id"]) if pd.notna(row["block_id"]) else None,
                    "created_at": str(row["created_at"]) if pd.notna(row["created_at"]) else "",
                    "event_type": str(row["event_type"]) if pd.notna(row["event_type"]) else "",
                    "rfid": rfid,
                }
            )
        return rows


class DbMigrations:
    """Table creation and one-shot migrations between kittyflap/kittyhack schemas."""

    @staticmethod
    def create_kittyhack_events_table(database: str):
        """Create the kittyhack ``events`` table if missing."""
        stmt = """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                block_id INTEGER,
                created_at DATETIME,
                event_type TEXT,
                original_image BLOB,
                modified_image BLOB,
                mouse_probability REAL,
                no_mouse_probability REAL,
                own_cat_probability REAL,
                rfid TEXT,
                event_text TEXT,
                img_width INTEGER,
                img_height INTEGER,
                effective_fps REAL,
                deleted BOOLEAN DEFAULT 0,
                thumbnail BLOB
            )
        """
        result = DatabaseCore.write_stmt_to_database(database, stmt)
        if result.success:
            logging.info(f"[DATABASE] Successfully created the 'events' table in the database '{database}'.")
        return result

    @staticmethod
    def create_visit_stats_tables(database: str) -> Result:
        """Create visit_stats and visit_stats_meta tables (and indexes) if missing."""
        stmt_stats = """
            CREATE TABLE IF NOT EXISTS visit_stats (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                cat_rfid TEXT,
                cat_name TEXT,
                conclusion TEXT NOT NULL,
                direction TEXT,
                passed INTEGER NOT NULL DEFAULT 0,
                prey INTEGER NOT NULL DEFAULT 0,
                denied_entry INTEGER NOT NULL DEFAULT 0,
                denied_exit INTEGER NOT NULL DEFAULT 0,
                attempt_no_pass INTEGER NOT NULL DEFAULT 0,
                uncertain_entry INTEGER NOT NULL DEFAULT 0,
                duration_outside_s REAL,
                duration_inside_s REAL,
                source_block_id INTEGER
            )
        """
        stmt_meta = """
            CREATE TABLE IF NOT EXISTS visit_stats_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """
        result = DatabaseCore.write_stmt_to_database(database, stmt_stats)
        if not result.success:
            return result
        result = DatabaseCore.write_stmt_to_database(database, stmt_meta)
        if not result.success:
            return result
        for idx_stmt in (
            "CREATE INDEX IF NOT EXISTS idx_visit_stats_created_at ON visit_stats (created_at)",
            "CREATE INDEX IF NOT EXISTS idx_visit_stats_cat_created ON visit_stats (cat_rfid, created_at)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_visit_stats_source_block ON visit_stats (source_block_id) WHERE source_block_id IS NOT NULL",
        ):
            result = DatabaseCore.write_stmt_to_database(database, idx_stmt)
            if not result.success:
                return result
        logging.info(f"[DATABASE] Ensured visit_stats tables in '{database}'.")
        return Result(True, "")

    @staticmethod
    def create_motion_timeline_table(database: str):
        """Create the motion_timeline table (one JSON timeline per motion block)."""
        stmt = """
            CREATE TABLE IF NOT EXISTS motion_timeline (
                block_id INTEGER PRIMARY KEY,
                timeline_json TEXT NOT NULL DEFAULT '[]'
            )
        """
        result = DatabaseCore.write_stmt_to_database(database, stmt)
        if result.success:
            logging.info(f"[DATABASE] Successfully created the 'motion_timeline' table in '{database}'.")
        return result

    @staticmethod
    def create_kittyhack_photo_table(database: str):
        """Create the legacy kittyhack ``photo`` table if missing."""
        result = DatabaseCore.lock_database()
        if not result.success:
            return result

        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS photo (
                    id INTEGER PRIMARY KEY,
                    created_at DATETIME,
                    blob_picture BLOB,
                    no_mouse_probability REAL,
                    mouse_probability REAL,
                    kittyflap_id INTEGER,
                    cat_id INTEGER,
                    rfid TEXT,
                    false_accept_probability REAL,
                    deleted INTEGER DEFAULT 0
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            error_message = f"An error occurred while creating the 'photo' table in the database '{database}': {e}"
            logging.error(error_message)
            return Result(False, error_message)
        else:
            logging.info(f"Successfully created the 'photo' table in the database '{database}'.")
            return Result(True, "")
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def create_kittyhack_cats_table(database: str):
        """Create the kittyhack ``cats`` table if missing."""
        stmt = """
            CREATE TABLE IF NOT EXISTS cats (
                id INTEGER PRIMARY KEY,
                created_at DATETIME,
                name TEXT,
                rfid TEXT,
                cat_image BLOB,
                enable_prey_detection INTEGER DEFAULT 1,
                allow_entry INTEGER DEFAULT 1,
                allow_exit INTEGER DEFAULT 1
            )
        """
        result = DatabaseCore.write_stmt_to_database(database, stmt)
        if result.success:
            logging.info(f"[DATABASE] Successfully created the 'cats' table in the database '{database}'.")
        return result

    @staticmethod
    def migrate_cats_to_kittyhack(kittyflap_db: str, kittyhack_db: str) -> Result:
        """Copy cats from legacy kittyflap ``cat`` table into kittyhack ``cats``."""
        result = DatabaseCore.lock_database()
        if not result.success:
            return result
        try:
            conn_src = sqlite3.connect(kittyflap_db, timeout=30)
            cursor_src = conn_src.cursor()
            conn_dst = sqlite3.connect(kittyhack_db, timeout=30)
            cursor_dst = conn_dst.cursor()

            cursor_src.execute("SELECT * FROM cat")
            src_db_rows = cursor_src.fetchall()
            for row in src_db_rows:
                # Source database columns: id, created_at, updated_at, deleted_at, last_updated_uuid, kittyflap_id, name, registered_at, rfid, profile_photo, registered_by_user_id, cat_config_id
                id, created_at, name, rfid, profile_photo = row[0], row[1], row[6], row[8], row[9]
                # Convert the 'profile_photo' text column to a BLOB
                # Decode the Base64 encoded profile photo to binary data
                try:
                    if profile_photo and not profile_photo.startswith(('http://', 'https://', '/')):
                        # Add padding if needed
                        missing_padding = len(profile_photo) % 4
                        if missing_padding:
                            profile_photo += '=' * (4 - missing_padding)
                        try:
                            cat_image = base64.b64decode(profile_photo)
                            img_array = np.frombuffer(cat_image, np.uint8)
                            img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
                            if img is not None:
                                cat_image = ImageUtil.resize_image_to_square(img, 800, 85)
                            else:
                                cat_image = None
                        except:
                            cat_image = None
                    else:
                        cat_image = None
                except Exception as e:
                    logging.warning(f"[DATABASE] Failed to decode profile photo: {e}")
                    cat_image = None
                cursor_dst.execute(
                    "INSERT INTO cats (id, created_at, name, rfid, cat_image) VALUES (?, ?, ?, ?, ?)",
                    (id, created_at, name, rfid, cat_image)
                )

            conn_dst.commit()
        except Exception as e:
            error_message = f"[DATABASE] An error occurred while migrating the 'cats' table from the database '{kittyflap_db}' to '{kittyhack_db}': {e}"
            logging.error(error_message)
            return Result(False, error_message)
        else:
            logging.info(f"[DATABASE] Successfully migrated the 'cats' table from the database '{kittyflap_db}' to '{kittyhack_db}'.")
            return Result(True, "")
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def migrate_photos_to_events(database: str) -> Result:    
        """Move deprecated ``photo`` rows into ``events`` (pre-v1.2 co-existence leftover)."""
        migrated_photos = 0
        result = DatabaseCore.lock_database()
        if not result.success:
            return result

        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM photo")
            photo_ids = cursor.fetchall()
            if not photo_ids:
                logging.info(f"[DATABASE] No photos to migrate from 'photo' table to 'events' table in the database '{database}'.")
                return Result(True, "")

            for photo_id in photo_ids:
                cursor.execute("SELECT * FROM photo WHERE id = ?", (photo_id[0],))
                photo = cursor.fetchone()
                if photo:
                    id, created_at, blob_picture, no_mouse_probability, mouse_probability, kittyflap_id, cat_id, rfid, false_accept_probability, deleted = photo
                    # Check if a photo with the same created_at timestamp already exists in the 'events' table
                    cursor.execute("SELECT id FROM events WHERE created_at = ?", (created_at,))
                    existing_event = cursor.fetchone()
                    if existing_event:
                        # If it exists, skip the migration and just delete the photo from the 'photo' table
                        cursor.execute("DELETE FROM photo WHERE id = ?", (id,))
                    else:
                        # If it does not exist, migrate the photo to the 'events' table
                        # Ensure that the 'id' is a unique identifier in the 'events' table. Set the 'id' to the max value of the 'events' table + 1.
                        cursor.execute("SELECT MAX(id) FROM events")
                        max_id = cursor.fetchone()[0]
                        if max_id is None:
                            max_id = 0
                        else:
                            max_id += 1

                        columns = "id, block_id, created_at, event_type, original_image, modified_image, mouse_probability, no_mouse_probability, rfid, event_text"
                        values = ', '.join(['?' for _ in columns.split(', ')])
                        values_list = [
                            max_id,
                            0,  # block_id is unknown, set to 0
                            created_at,
                            "image",
                            blob_picture,
                            None,  # modified_image does not exist in the 'photo' table
                            mouse_probability,
                            no_mouse_probability,
                            rfid,
                            ""  # event_text
                        ]
                        cursor.execute(f"INSERT INTO events ({columns}) VALUES ({values})", values_list)
                        cursor.execute(f"DELETE FROM photo WHERE id = ?", (id,))
                        migrated_photos += 1

                    if migrated_photos > 0:
                        logging.info(f"[DATABASE] Migrated {migrated_photos} photos from 'photo' table to 'events' table in the database '{database}'.")
                        # Rewrite the 'id' column in the 'events' table based on the ascending order of the 'created_at' column
                        # Create a temporary table to store the new IDs
                        cursor.execute("CREATE TEMPORARY TABLE temp_events (old_id INTEGER, new_id INTEGER)")
                        cursor.execute("INSERT INTO temp_events (old_id, new_id) SELECT id, ROW_NUMBER() OVER (ORDER BY created_at) FROM events")

                        # Log the ID changes
                        cursor.execute("SELECT old_id, new_id FROM temp_events")

                        # Update the original table with the new IDs
                        cursor.execute("UPDATE events SET id = (SELECT new_id FROM temp_events WHERE old_id = events.id)")

                        # Drop the temporary table
                        cursor.execute("DROP TABLE temp_events")

            conn.commit()
            conn.close()
        except Exception as e:
            error_message = f"[DATABASE] An error occurred while migrating photos from 'photo' table to 'events' table in the database '{database}': {e}"
            logging.error(error_message)
            return Result(False, error_message)
        else:
            logging.info(f"[DATABASE] Successfully migrated photos from 'photo' table to 'events' table in the database '{database}'.")
            return Result(True, "")
        finally:
            DatabaseCore.release_database()

    @staticmethod
    def clear_original_kittyflap_database(database: str) -> Result:
        """Delete legacy kittyflap ``photo`` / ``kportal_request`` rows and VACUUM."""
        result = DatabaseCore.lock_database()
        if not result.success:
            return result

        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM photo")
            cursor.execute("DELETE FROM kportal_request")
            conn.commit()
            cursor.execute("VACUUM")
            conn.close()
        except Exception as e:
            error_message = f"[DATABASE] An error occurred while clearing the database '{database}': {e}"
            logging.error(error_message)
            return Result(False, error_message)
        else:
            logging.info(f"[DATABASE] Successfully cleared the database '{database}'.")
            return Result(True, "")
        finally:
            DatabaseCore.release_database()


# ---------------------------------------------------------------------------
# Compatibility aliases (keep `from src.database import *` working)
# ---------------------------------------------------------------------------
lock_database = DatabaseCore.lock_database
release_database = DatabaseCore.release_database
read_df_from_database = DatabaseCore.read_df_from_database
read_column_info_from_database = DatabaseCore.read_column_info_from_database
write_stmt_to_database = DatabaseCore.write_stmt_to_database
db_get_config = DatabaseCore.db_get_config
db_set_config = DatabaseCore.db_set_config
create_index_on_events = DatabaseCore.create_index_on_events
vacuum_database = DatabaseCore.vacuum_database
check_if_table_exists = DatabaseCore.check_if_table_exists
check_if_column_exists = DatabaseCore.check_if_column_exists
add_column_to_table = DatabaseCore.add_column_to_table
check_database_integrity = DatabaseCore.check_database_integrity
backup_database_sqlite = DatabaseCore.backup_database_sqlite
get_jpeg_size = EventsRepo.get_jpeg_size
update_image_dimensions_for_block = EventsRepo.update_image_dimensions_for_block
_event_bundle_paths = EventsRepo._event_bundle_paths
_remove_event_bundle_files = EventsRepo._remove_event_bundle_files
_original_image_path = EventsRepo._original_image_path
_thumbnail_image_path = EventsRepo._thumbnail_image_path
_remove_event_image_files = EventsRepo._remove_event_image_files
_make_placeholder_image = EventsRepo._make_placeholder_image
db_get_photos = EventsRepo.db_get_photos
db_get_photos_by_block_id = EventsRepo.db_get_photos_by_block_id
db_count_photos = EventsRepo.db_count_photos
get_ids_without_thumbnail = EventsRepo.get_ids_without_thumbnail
get_thubmnail_by_id = EventsRepo.get_thubmnail_by_id
write_motion_timeline = EventsRepo.write_motion_timeline
delete_motion_timeline_by_block_id = EventsRepo.delete_motion_timeline_by_block_id
db_get_motion_timelines = EventsRepo.db_get_motion_timelines
read_photo_by_id = EventsRepo.read_photo_by_id
delete_photo_by_id = EventsRepo.delete_photo_by_id
delete_photos_by_block_id = EventsRepo.delete_photos_by_block_id
create_json_from_event = EventsRepo.create_json_from_event
read_event_from_json = EventsRepo.read_event_from_json
get_detected_object_by_index = EventsRepo.get_detected_object_by_index
write_motion_block_to_db = EventsRepo.write_motion_block_to_db
cleanup_orphan_image_files = EventsRepo.cleanup_orphan_image_files
purge_excess_photos = EventsRepo.purge_excess_photos
db_get_motion_blocks = EventsRepo.db_get_motion_blocks
cleanup_deleted_events = EventsRepo.cleanup_deleted_events
get_ids_with_original_blob = EventsRepo.get_ids_with_original_blob
perform_event_image_migration_ids = EventsRepo.perform_event_image_migration_ids
db_get_cats = CatsRepo.db_get_cats
db_get_all_rfid_tags = CatsRepo.db_get_all_rfid_tags
db_delete_cat_by_id = CatsRepo.db_delete_cat_by_id
db_update_cat_data_by_id = CatsRepo.db_update_cat_data_by_id
db_add_new_cat = CatsRepo.db_add_new_cat
get_cat_settings_map = CatsRepo.get_cat_settings_map
get_cat_name_rfid_dict = CatsRepo.get_cat_name_rfid_dict
get_cat_names_list = CatsRepo.get_cat_names_list
get_cat_thumbnail = CatsRepo.get_cat_thumbnail
create_kittyhack_events_table = DbMigrations.create_kittyhack_events_table
create_motion_timeline_table = DbMigrations.create_motion_timeline_table
create_visit_stats_tables = DbMigrations.create_visit_stats_tables
create_kittyhack_photo_table = DbMigrations.create_kittyhack_photo_table
create_kittyhack_cats_table = DbMigrations.create_kittyhack_cats_table
migrate_cats_to_kittyhack = DbMigrations.migrate_cats_to_kittyhack
migrate_photos_to_events = DbMigrations.migrate_photos_to_events
clear_original_kittyflap_database = DbMigrations.clear_original_kittyflap_database
