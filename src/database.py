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
    get_utc_date_string,
    process_image,
    resize_image_to_square,
    get_free_disk_space,
    get_database_size,
    get_file_size,
    Result
    )
from src.camera import image_buffer, DetectedObject

# -----------------------------------------------------------------------------
# Filesystem storage for original images and thumbnails (v2.4+)
# New events will no longer store 'original_image' and 'thumbnail' as BLOBs.
# Instead they are written as JPEG files to the filesystem. Filenames are
# <id>.jpg where <id> is the autoincrement primary key of the event row.
# Backward compatibility: if a file is missing we fall back to the legacy
# BLOBs stored in the database (for older rows) when reading.
# -----------------------------------------------------------------------------
ORIGINAL_IMAGE_DIR = "/root/pictures/original_images"
THUMBNAIL_DIR = "/root/pictures/thumbnails"
os.makedirs(ORIGINAL_IMAGE_DIR, exist_ok=True)
os.makedirs(THUMBNAIL_DIR, exist_ok=True)

# Event frame bundles (generated on-demand for the event modal)
EVENT_BUNDLE_DIR = os.path.join(THUMBNAIL_DIR, "bundles")
os.makedirs(EVENT_BUNDLE_DIR, exist_ok=True)

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


def update_image_dimensions_for_block(database: str, block_id: int, width: int, height: int) -> Result:
    """Store image dimensions for an event block if columns exist and values are missing."""
    try:
        if not check_if_column_exists(database, "events", "img_width") or not check_if_column_exists(database, "events", "img_height"):
            return Result(True, "")
    except Exception:
        return Result(True, "")

    result = lock_database()
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
        release_database()

def _event_bundle_paths(block_id: int) -> List[str]:
    """Return possible bundle paths for a block (supports legacy .tar.gz and current .tar)."""
    bid = int(block_id)
    return [
        os.path.join(EVENT_BUNDLE_DIR, f"event_{bid}.tar"),
        os.path.join(EVENT_BUNDLE_DIR, f"event_{bid}.tar.gz"),
    ]

def _remove_event_bundle_files(block_ids: Iterable[int]) -> int:
    removed = 0
    for bid in set(int(b) for b in block_ids if b is not None):
        for p in _event_bundle_paths(bid):
            try:
                if os.path.exists(p):
                    os.remove(p)
                    removed += 1
                    logging.debug(f"[DATABASE] Removed event bundle file '{p}'.")
            except Exception as e:
                logging.warning(f"[DATABASE] Failed removing event bundle for block_id {bid} ('{p}'): {e}")
    return removed

def _original_image_path(image_id: int) -> str:
    return os.path.join(ORIGINAL_IMAGE_DIR, f"{image_id}.jpg")

def _thumbnail_image_path(image_id: int) -> str:
    return os.path.join(THUMBNAIL_DIR, f"{image_id}.jpg")

def _remove_event_image_files(ids: Iterable[int]) -> tuple[int, int]:
    """
    Remove filesystem-stored original and thumbnail JPGs for the given event IDs.
    Returns a tuple (removed_originals, removed_thumbnails).
    """
    removed_orig = 0
    removed_thumb = 0
    for pid in set(int(i) for i in ids):
        # Original
        try:
            orig_path = _original_image_path(pid)
            if os.path.exists(orig_path):
                os.remove(orig_path)
                removed_orig += 1
                logging.debug(f"[DATABASE] Removed original image file '{orig_path}'.")
        except Exception as e:
            logging.warning(f"[DATABASE] Failed removing original image file for ID {pid}: {e}")
        # Thumbnail
        try:
            thumb_path = _thumbnail_image_path(pid)
            if os.path.exists(thumb_path):
                os.remove(thumb_path)
                removed_thumb += 1
                logging.debug(f"[DATABASE] Removed thumbnail file '{thumb_path}'.")
        except Exception as e:
            logging.warning(f"[DATABASE] Failed removing thumbnail file for ID {pid}: {e}")
    return removed_orig, removed_thumb

def _make_placeholder_image(text: str, size=(640, 480), bg=(230, 230, 230), fg=(30, 30, 30)) -> bytes:
    """
    Create a simple JPEG placeholder with a message.
    """
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

# database actions
class db_action(Enum):
    get_photos = 0
    get_photos_table = 1
    get_photos_ids = 2
    get_cats = 3
    get_cats_table = 4
    get_config = 5
    get_config_table = 6

# Return types for photo database actions
class ReturnDataPhotosDB(Enum):
    all = 0
    all_except_photos = 1
    only_ids = 2
    all_modified_image = 3
    all_original_image = 4

# Return types for cat database actions
class ReturnDataCatDB(Enum):
    all = 0
    all_except_photos = 1

# Return types for config database actions
class ReturnDataConfigDB(Enum):
    all = 0
    all_except_password = 1

# Detection object and Event data schema
class DetectedObjectSchema(TypedDict):
    object_name: str
    probability: float
    x: float
    y: float
    width: float
    height: float

class EventSchema(TypedDict):
    detected_objects: List[DetectedObjectSchema]
    event_text: str

class LastImageBlockTimestamp:
    _timestamp = tm.time()
    _lock = Lock()

    @classmethod
    def get_timestamp(cls):
        with cls._lock:
            return cls._timestamp

    @classmethod
    def update_timestamp(cls, timestamp: float):
        with cls._lock:
            cls._timestamp = timestamp

# Initialize the timestamp class
last_imgblock_ts = LastImageBlockTimestamp()

_cat_thumbnail_cache = {}

# Lock for database writes
db_write_lock = Lock()

def lock_database(timeout: int = 60, check_interval: float = 0.1) -> Result:
    """
    This function checks if the database is locked (db_write_lock). If it is locked,
    the function waits up to a given time and checks periodically if the lock is released.
    If the lock is not released after the given time, the function returns an error message.

    :param timeout: Maximum time to wait for the lock to be released (in seconds).
    :param check_interval: Time interval between lock checks (in seconds).
    :return: Result(success: bool, error_message: str)
    """
    start_time = tm.time()
    while tm.time() - start_time < timeout:
        if db_write_lock.acquire(blocking=False):
            logging.debug("[DATABASE] Database lock acquired.")
            return Result(True, "")
        tm.sleep(check_interval)
    error_message = f"[DATABASE] Database lock not released within the given timeout ({timeout}s)."
    logging.error(error_message)
    return Result(False, error_message)

def release_database():
    """
    This function releases the database lock (db_write_lock) after a write operation is done.
    """
    if db_write_lock.locked():
        db_write_lock.release()
        logging.debug("[DATABASE] Database lock released.")
    else:
        logging.warning("[DATABASE] Database lock is not acquired. Nothing to release.")

###### General database operations ######

def read_df_from_database(database: str, stmt: str) -> pd.DataFrame:
    result = lock_database()
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
        release_database()
        
    return df

def read_column_info_from_database(database: str, table: str):
    result = lock_database()
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
        release_database()

    return columns_info
    
def write_stmt_to_database(database: str, stmt: str) -> Result:
    """
    this function writes the statement to the database.

    returns @dataclass Result(success: bool, error_message: str)
    """
    result = lock_database()
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
        release_database()


###### Specific database operations ######

def db_get_photos(database: str, 
                  return_data: ReturnDataPhotosDB, 
                  date_start="2020-01-01 00:00:00", 
                  date_end="2100-12-31 23:59:59", 
                  cats_only=False, 
                  mouse_only=False, 
                  mouse_probability=0.0, 
                  page_index = 0, 
                  elements_per_page = sys.maxsize,
                  ignore_deleted = True):
    """
    this function returns all dataframes from the 'events' table, based on the
    specified filter.
    If no filters are specified, this function returns all avaliable dataframes.
    The newest data are at the top of the dataframe.
    """
    if return_data == ReturnDataPhotosDB.all:
         columns = "id, block_id, created_at, event_type, original_image, modified_image, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text"
    elif return_data == ReturnDataPhotosDB.all_modified_image:
         columns = "id, block_id, created_at, event_type, modified_image, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text"
    elif return_data == ReturnDataPhotosDB.all_original_image:
         columns = "id, block_id, created_at, event_type, original_image, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text"
    elif return_data == ReturnDataPhotosDB.all_except_photos:
         columns = "id, block_id, created_at, event_type, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text"
    elif return_data == ReturnDataPhotosDB.only_ids:
         columns = "id"
    else:
         columns = "*"
    
    # Check if 'deleted' column exists (this column exists only in the kittyhack database)
    columns_info = read_column_info_from_database(database, "events")
    column_names = [info[1] for info in columns_info]
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
        # calculate the total number of pages
        total_rows = read_df_from_database(database, f"SELECT COUNT(*) as count FROM ({stmt})").iloc[0]['count']
        total_pages = (total_rows + elements_per_page - 1) // elements_per_page
        # calculate the offset for the current page
        offset = (total_pages - page_index - 1) * elements_per_page
        stmt = f"{stmt} LIMIT {elements_per_page} OFFSET {offset}"

    logging.debug(f"[DATABASE] query db_get_photos: return_data={return_data}, date_start={date_start}, date_end={date_end}, cats_only={cats_only}, mouse_only={mouse_only}, mouse_probability={mouse_probability}, page_index={page_index}, elements_per_page={elements_per_page}, ignore_deleted={ignore_deleted}")

    df = read_df_from_database(database, stmt)

    # Inject filesystem stored original images (backward compatibility)
    if not df.empty and 'original_image' in df.columns:
        for idx, row in df.iterrows():
            if row.get('original_image') is None:
                img_path = _original_image_path(int(row['id'])) if 'id' in row else None
                if img_path and os.path.exists(img_path):
                    try:
                        with open(img_path, 'rb') as f:
                            df.at[idx, 'original_image'] = f.read()
                    except Exception as e:
                        logging.warning(f"[DATABASE] Failed to read original image file '{img_path}': {e}")

    return df

def db_get_photos_by_block_id(
    database: str,
    block_id: int,
    return_data: ReturnDataPhotosDB = ReturnDataPhotosDB.all,
    ignore_deleted: bool = True,
):
    """
    this function returns all dataframes from the 'events' table, based on the
    specified block_id.
    
    :param database: Path to the database file
    :param block_id: ID of the block to retrieve
    :param return_data: Type of data to return (ReturnDataPhotosDB enum)
    :return: DataFrame containing the requested data
    """
    columns_info = read_column_info_from_database(database, "events")
    column_names = set(info[1] for info in (columns_info or []))
    dims_cols = ", img_width, img_height" if ('img_width' in column_names and 'img_height' in column_names) else ""

    if return_data == ReturnDataPhotosDB.all:
        columns = "id, block_id, created_at, event_type, original_image, modified_image, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text" + dims_cols + ", thumbnail"
    elif return_data == ReturnDataPhotosDB.all_modified_image:
        columns = "id, block_id, created_at, event_type, modified_image, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text" + dims_cols + ", thumbnail"
    elif return_data == ReturnDataPhotosDB.all_original_image:
        columns = "id, block_id, created_at, event_type, original_image, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text" + dims_cols + ", thumbnail"
    elif return_data == ReturnDataPhotosDB.all_except_photos:
        columns = "id, block_id, created_at, event_type, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text" + dims_cols
    elif return_data == ReturnDataPhotosDB.only_ids:
        columns = "id"
        
    # Check if 'deleted' column exists (this column exists only in the kittyhack database)
    if 'deleted' in column_names and ignore_deleted is True:
        stmt = f"SELECT {columns} FROM events WHERE block_id = {block_id} AND deleted != 1"
    else:
        stmt = f"SELECT {columns} FROM events WHERE block_id = {block_id}"
    df = read_df_from_database(database, stmt)

    if not df.empty and 'original_image' in df.columns:
        for idx, row in df.iterrows():
            if row.get('original_image') is None:
                img_path = _original_image_path(int(row['id'])) if 'id' in row else None
                if img_path and os.path.exists(img_path):
                    try:
                        with open(img_path, 'rb') as f:
                            df.at[idx, 'original_image'] = f.read()
                    except Exception as e:
                        logging.warning(f"[DATABASE] Failed to read original image file '{img_path}': {e}")

    if not df.empty and 'thumbnail' in df.columns:
        for idx, row in df.iterrows():
            if row.get('thumbnail') is None:
                thumb_path = _thumbnail_image_path(int(row['id'])) if 'id' in row else None
                if thumb_path and os.path.exists(thumb_path):
                    try:
                        with open(thumb_path, 'rb') as f:
                            df.at[idx, 'thumbnail'] = f.read()
                    except Exception as e:
                        logging.warning(f"[DATABASE] Failed to read thumbnail file '{thumb_path}': {e}")
    return df


def db_count_photos(
    database: str,
    date_start: str = "2020-01-01 00:00:00",
    date_end: str = "2100-12-31 23:59:59",
    cats_only: bool = False,
    mouse_only: bool = False,
    mouse_probability: float = 0.0,
    ignore_deleted: bool = True,
) -> int:
    """Count photos in the events table matching the given filters.

    This is a lightweight alternative to loading all IDs via db_get_photos(...only_ids).
    """
    try:
        where = f"created_at BETWEEN '{date_start}' AND '{date_end}'"

        # Check if 'deleted' column exists (this column exists only in the kittyhack database)
        columns_info = read_column_info_from_database(database, "events")
        column_names = [info[1] for info in columns_info]
        if 'deleted' in column_names and ignore_deleted:
            where += " AND deleted != 1"

        if mouse_only:
            where += f" AND mouse_probability >= {float(mouse_probability)}"
        if cats_only:
            where += " AND rfid != ''"

        stmt = f"SELECT COUNT(*) AS count FROM events WHERE {where}"
        df = read_df_from_database(database, stmt)
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

def get_ids_without_thumbnail(database: str):
    """
    This function returns the IDs of the images that do not have a thumbnail.
    
    :param database: Path to the database file
    :return: List of IDs without thumbnails
    """
    stmt = "SELECT id FROM events WHERE thumbnail IS NULL AND deleted != 1"
    df = read_df_from_database(database, stmt)
    if df.empty:
        return []
    result_ids = []
    for __, row in df.iterrows():
        img_id = int(row['id'])
        thumb_path = _thumbnail_image_path(img_id)
        # Only report IDs where both DB thumbnail is NULL and no file exists
        if not os.path.exists(thumb_path):
            result_ids.append(img_id)
    return result_ids

def get_thubmnail_by_id(database: str, photo_id: int):
    """
    This function reads a specific thumbnail image based on the ID from the source database.
    If no thumbnail exists, it creates one from the original image.
    """
    thumb_path = _thumbnail_image_path(photo_id)
    if os.path.exists(thumb_path):
        try:
            with open(thumb_path, 'rb') as f:
                return f.read()
        except Exception as e:
            logging.warning(f"[DATABASE] Failed to read thumbnail file '{thumb_path}': {e}")

    # Legacy DB fallback / or need to create new thumbnail
    stmt = f"SELECT thumbnail, original_image FROM events WHERE id = {photo_id}"
    df = read_df_from_database(database, stmt)
    if df.empty:
        logging.error(f"[DATABASE] Photo with ID {photo_id} not found")
        # Return a placeholder thumbnail
        return _make_placeholder_image("Image not found", size=(320, 240))

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
    orig_path = _original_image_path(photo_id)
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
        thumbnail = _make_placeholder_image("Image not found", size=(320, 240))
        try:
            with open(thumb_path, 'wb') as f:
                f.write(thumbnail)
        except Exception as e:
            logging.warning(f"[DATABASE] Failed to write placeholder thumbnail '{thumb_path}': {e}")
        return thumbnail

    try:
        thumbnail = process_image(original_image, 640, 480, 50)
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
        return _make_placeholder_image("Image not found", size=(320, 240))

def db_get_cats(database: str, return_data: ReturnDataCatDB):
    """
    this function returns all dataframes from the 'cats' table.
    """
    if return_data == ReturnDataCatDB.all:
         columns = "*"
    elif return_data == ReturnDataCatDB.all_except_photos:
        columns = "id, created_at, name, rfid, enable_prey_detection, allow_entry, allow_exit"

    stmt = f"SELECT {columns} FROM cats"
    return read_df_from_database(database, stmt)

def db_get_all_rfid_tags(database: str):
    """
    This function returns all RFID tags from the 'cats' table as an array.
    If a cat's RFID is empty, its name (lowercase) is used instead.
    """
    stmt = "SELECT rfid, name FROM cats"
    df = read_df_from_database(database, stmt)
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

def db_get_config(database: str, return_data: ReturnDataConfigDB):
    """
    this function returns all dataframes from the 'config' table.
    """
    if return_data == ReturnDataConfigDB.all:
         columns = "*"
    elif return_data == ReturnDataConfigDB.all_except_password:
        columns = "id, wifi_ssid, ip, acceptance_rate, cat_prob_threshold, accept_all_cats, detect_prey"

    stmt = f"SELECT {columns} FROM config"
    return read_df_from_database(database, stmt)

def db_set_config(database: str, 
                  updated_at: datetime, 
                  acceptance_rate: float, 
                  accept_all_cats: bool, 
                  detect_prey: bool, 
                  cat_prob_threshold: float) -> Result:
    """
    this function writes the configuration data to the database.
    """
    data = f"updated_at = '{updated_at}', acceptance_rate = {acceptance_rate}, accept_all_cats = {int(accept_all_cats)}, detect_prey = {int(detect_prey)}, cat_prob_threshold = {cat_prob_threshold}"
    logging.info(f"[DATABASE] Writing new kittyflap configuration to 'config' table in database '{database}': {data}")
    stmt = f"UPDATE config SET {data} WHERE id = (SELECT id FROM config LIMIT 1)"
    result = write_stmt_to_database(database, stmt)
    if result.success == True:
        logging.info("[DATABASE] Kittyflap configuration updated successfully.")
    return result

def create_kittyhack_events_table(database: str):
    """
    This function creates the 'events' table in 
    the destination database if it does not exist.
    """
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
            deleted BOOLEAN DEFAULT 0,
            thumbnail BLOB
        )
    """
    result = write_stmt_to_database(database, stmt)
    if result.success:
        logging.info(f"[DATABASE] Successfully created the 'events' table in the database '{database}'.")
    return result

def create_kittyhack_photo_table(database: str):
    """
    This function creates the 'photo' table (kittyhack specific style) in 
    the destination database if it does not exist.
    """
    result = lock_database()
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
        release_database()

def create_kittyhack_cats_table(database: str):
    """
    This function creates the 'cats' table in 
    the destination database if it does not exist.
    """
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
    result = write_stmt_to_database(database, stmt)
    if result.success:
        logging.info(f"[DATABASE] Successfully created the 'cats' table in the database '{database}'.")
    return result

def migrate_cats_to_kittyhack(kittyflap_db: str, kittyhack_db: str) -> Result:
    """
    This function migrates the 'cats' table from the kittyflap database to the kittyhack database.
    """
    result = lock_database()
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
                            cat_image = resize_image_to_square(img, 800, 85)
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
        release_database()

def db_delete_cat_by_id(database: str, cat_id: int) -> Result:
    """
    This function deletes a specific cat based on the ID from the source database.
    """
    stmt = f"DELETE FROM cats WHERE id = {cat_id}"
    result = write_stmt_to_database(database, stmt)
    if result.success == True:
        logging.info(f"[DATABASE] Cat with ID '{cat_id}' deleted successfully.")
    return result

def db_update_cat_data_by_id(database: str, cat_id: int, name: str, rfid: str, cat_image_path: str, enable_prey_detection: bool = True, allow_entry: bool = True, allow_exit: bool = True) -> Result:
    """
    This function updates the cat data based on the ID from the source database.
    If the image should not be updated, set cat_image_path to None.
    """
    if cat_image_path:
        try:
            img = cv2.imread(cat_image_path)
            cat_image_blob = resize_image_to_square(img, 800, 85)
        except Exception as e:
            error_message = f"[DATABASE] Failed to read image file '{cat_image_path}': {e}"
            logging.error(error_message)
            return Result(False, error_message)
    else:
        cat_image_blob = None

    result = lock_database()
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
        release_database()

def db_add_new_cat(database: str, name: str, rfid: str, cat_image_path: str,
                   enable_prey_detection: bool = True, allow_entry: bool = True, allow_exit: bool = True) -> Result: 
    """
    This function adds a new cat to the database.
    The cat_image_path should be the path to the image file (jpg).
    """
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

    result = lock_database()
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
            (id, get_utc_date_string(tm.time()), name, rfid, cat_image_blob, int(enable_prey_detection), int(allow_entry), int(allow_exit))
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
        release_database()

def get_cat_settings_map(database: str) -> dict:
    """
    Returns a mapping from RFID (or fallback name.lower() if RFID empty) to a dict with per-cat settings:
    { 'enable_prey_detection': bool, 'allow_entry': bool, 'allow_exit': bool }
    """
    try:
        df = read_df_from_database(database, "SELECT name, rfid, enable_prey_detection, allow_entry, allow_exit FROM cats")
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

def read_photo_by_id(database: str, photo_id: int) -> pd.DataFrame:
    """
    This function reads a specific dataframe based on the ID from the source database.
    """
    columns = "id, block_id, created_at, event_type, original_image, modified_image, mouse_probability, no_mouse_probability, own_cat_probability, rfid, event_text"
    stmt = f"SELECT {columns} FROM events WHERE id = {photo_id}"
    df = read_df_from_database(database, stmt)
    if not df.empty and 'original_image' in df.columns:
        row = df.iloc[0]
        if row.get('original_image') is None:
            img_path = _original_image_path(int(row['id']))
            if os.path.exists(img_path):
                try:
                    with open(img_path, 'rb') as f:
                        df.at[0, 'original_image'] = f.read()
                except Exception as e:
                    logging.warning(f"[DATABASE] Failed to read original image file '{img_path}': {e}")
            else:
                # Provide a placeholder original image instead of None
                logging.error(f"[DATABASE] Original image not found for photo ID {photo_id}")
                df.at[0, 'original_image'] = _make_placeholder_image("Image not found", size=(640, 480))
    return df

def delete_photo_by_id(database: str, photo_id: int) -> Result:
    """
    This function deletes a specific dataframe based on the ID from the source database
    and removes the corresponding image files from the filesystem.
    """
    # Fetch block_id first so we can invalidate its bundle.
    block_id: int | None = None
    try:
        df_block = read_df_from_database(database, f"SELECT block_id FROM events WHERE id = {photo_id}")
        if not df_block.empty and 'block_id' in df_block.columns:
            block_id = int(df_block.iloc[0]['block_id'])
    except Exception as e:
        logging.debug(f"[DATABASE] Failed reading block_id for photo ID {photo_id}: {e}")

    stmt = f"UPDATE events SET original_image = NULL, modified_image = NULL, thumbnail = NULL, deleted = 1 WHERE id = {photo_id}"
    result = write_stmt_to_database(database, stmt)
    if result.success is True:
        removed_orig, removed_thumb = _remove_event_image_files([photo_id])
        removed_bundles = 0
        if block_id is not None:
            removed_bundles = _remove_event_bundle_files([block_id])
        logging.info(
            f"[DATABASE] Photo with ID '{photo_id}' deleted "
            f"(filesystem removed: originals={removed_orig}, thumbnails={removed_thumb}, bundles={removed_bundles})."
        )
    return result

def delete_photos_by_block_id(database: str, block_id: int) -> Result:
    """
    This function deletes all dataframes based on the block_id from the source database
    and removes corresponding image files from the filesystem.
    """
    # Collect IDs in this block first
    df_ids = read_df_from_database(database, f"SELECT id FROM events WHERE block_id = {block_id}")
    ids = df_ids['id'].tolist() if not df_ids.empty else []

    stmt = f"UPDATE events SET original_image = NULL, modified_image = NULL, thumbnail = NULL, deleted = 1 WHERE block_id = {block_id}"
    result = write_stmt_to_database(database, stmt)
    if result.success is True:
        removed_orig, removed_thumb = _remove_event_image_files(ids)
        removed_bundles = _remove_event_bundle_files([block_id])
        logging.info(
            f"[DATABASE] Photos with block ID '{block_id}' deleted "
            f"(filesystem removed: originals={removed_orig}, thumbnails={removed_thumb}, bundles={removed_bundles})."
        )
    return result

def create_json_from_event(detected_objects: List[DetectedObject]) -> str:
    """
    Create a JSON string from a list of detected objects.
    
    :param detected_objects: List of DetectedObject instances
    :return: JSON string containing the event data
    """
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

def read_event_from_json(event_json: str) -> List[DetectedObject]:
    """
    Parse a JSON string containing event data back into a list of DetectedObject instances.
    
    :param event_json: JSON string containing the event data
    :return: List of DetectedObject instances
    """
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
    
def get_detected_object_by_index(detected_objects: List[DetectedObject], index: int) -> DetectedObject:
    """
    This function returns the values of a detected_object from a DetectedObject list.
    
    :param detected_objects: List of DetectedObject instances
    :param index: Index of the detected object to return
    :return: DetectedObject instance
    """
    if index < len(detected_objects):
        return detected_objects[index]
    return None

def write_motion_block_to_db(database: str, buffer_block_id: int, event_type: str = "image", delete_from_buffer: bool = True, generate_thumbnails: bool = True):
    """
    This function writes an image block from the image buffer to the database.
    """
    result = lock_database()
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

        # Determine whether dimension columns exist (avoid nested lock acquisition).
        try:
            cursor.execute("PRAGMA table_info(events)")
            _cols = set(r[1] for r in cursor.fetchall())
            has_dims = ('img_width' in _cols and 'img_height' in _cols)
        except Exception:
            has_dims = False

        # Decide the max number of pictures to write to the database, based on the content of the first element.tag_id
        # (every element of the block has the same tag_id)
        max_images = CONFIG['MAX_PICTURES_PER_EVENT_WITH_RFID'] if elements[0].tag_id else CONFIG['MAX_PICTURES_PER_EVENT_WITHOUT_RFID']

        index = 0
        for element in elements:
            # Write the image to the database, if the index is less than the maximum number of images
            if index < max_images:
                try:
                    detected_objects = element.detected_objects if element.detected_objects is not None else []
                    event_json = create_json_from_event(detected_objects)
                except Exception as e:
                    event_json = json.dumps({'detected_objects': [], 'event_text': ''})
                    logging.error(f"[DATABASE] Failed to serialize event data: {e}")

                img_w = None
                img_h = None
                if has_dims and element.original_image is not None:
                    try:
                        s = get_jpeg_size(element.original_image)
                        if s:
                            img_w, img_h = int(s[0]), int(s[1])
                    except Exception:
                        pass

                columns = "block_id, created_at, event_type, original_image, modified_image, mouse_probability, no_mouse_probability, own_cat_probability, rfid, event_text"
                if has_dims:
                    columns += ", img_width, img_height"
                values = ', '.join(['?' for _ in columns.split(', ')])
                values_list = [
                    db_block_id,
                    get_utc_date_string(element.timestamp),
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
                cursor.execute(f"INSERT INTO events ({columns}) VALUES ({values})", values_list)
                new_row_id = cursor.lastrowid
                # Persist original image to filesystem (if available)
                if element.original_image is not None:
                    orig_path = _original_image_path(new_row_id)
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
        cursor.execute("SELECT COUNT(*) FROM events WHERE deleted != 1")
        total_photos = cursor.fetchone()[0]
        if 'MAX_PHOTOS_COUNT' in CONFIG and total_photos > CONFIG['MAX_PHOTOS_COUNT']:
            excess_photos = total_photos - CONFIG['MAX_PHOTOS_COUNT']
            logging.info(f"[DATABASE] Number of photos exceeds limit. Deleting {excess_photos} oldest photos.")
            cursor.execute(f"SELECT id, created_at, block_id FROM events WHERE deleted != 1 ORDER BY created_at ASC LIMIT {excess_photos}")
            photos_to_delete = cursor.fetchall()
            ids_to_purge: List[int] = []
            blocks_to_invalidate: List[int] = []
            for photo in photos_to_delete:
                photo_id = photo[0]
                logging.debug(f"[DATABASE] Deleting photo ID: {photo_id}, created_at: {photo[1]}")
                try:
                    blocks_to_invalidate.append(int(photo[2]))
                except Exception:
                    pass
                cursor.execute("UPDATE events SET deleted = 1, original_image = NULL, modified_image = NULL, thumbnail = NULL WHERE id = ?", (photo_id,))
                ids_to_purge.append(photo_id)

            # Remove filesystem-stored original image and thumbnail for purged IDs
            if ids_to_purge:
                removed_orig, removed_thumb = _remove_event_image_files(ids_to_purge)
                removed_bundles = _remove_event_bundle_files(blocks_to_invalidate)
                logging.info(
                    f"[DATABASE] Purged oldest photos "
                    f"(filesystem removed: originals={removed_orig}, thumbnails={removed_thumb}, bundles={removed_bundles})."
                )

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
        release_database()

    if generate_thumbnails:
        db_photo_ids = db_get_photos_by_block_id(database, db_block_id, ReturnDataPhotosDB.only_ids)
        for db_photo_id in db_photo_ids['id']:
            get_thubmnail_by_id(database, db_photo_id)
        logging.info(f"[DATABASE] Generated {len(db_photo_ids)} thumbnails for block ID '{db_block_id}'. (Filesystem storage)")

def cleanup_orphan_image_files(database: str) -> Result:
    """
    Remove JPG files in ORIGINAL_IMAGE_DIR and THUMBNAIL_DIR that have no corresponding event id in DB.
    Considers:
      - files named '<id>.jpg' where <id> not in active event IDs (deleted != 1)
      - any other '*.jpg' file whose stem is NOT a pure integer (e.g. 'asdf.jpg'): treated as orphan
    Safe to run periodically.
    """
    try:
        # Collect valid IDs from DB (include non-deleted only)
        result = lock_database()
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
            release_database()

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
    
def create_index_on_events(database: str) -> Result:
    """
    This function creates indexes in the events and cats tables.
    """
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_id ON events (id)",
        "CREATE INDEX IF NOT EXISTS idx_block_id_created_at ON events (block_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_events_block_id_deleted_created_at ON events (block_id, deleted, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_cats_rfid ON cats (rfid)"
    ]
    
    for stmt in indexes:
        result = write_stmt_to_database(database, stmt)
        if not result.success:
            return result
    
    logging.info("[DATABASE] Successfully created indexes.")
    return Result(True, "")

def db_get_motion_blocks(database: str, block_count: int = 0, date_start="2020-01-01 00:00:00", date_end="2100-12-31 23:59:59", cats_only=False, mouse_only=False, mouse_probability=0.0):
    """
    This function reads the last 'block_count' motion blocks from the database with specified filters.

    :param database: Path to the database file
    :param block_count: Number of motion blocks to return (0 for all)
    :param date_start: Start date for filtering (format: 'YYYY-MM-DD HH:MM:SS')
    :param date_end: End date for filtering (format: 'YYYY-MM-DD HH:MM:SS')
    :param cats_only: If True, only return blocks with RFID tags
    :param mouse_only: If True, only return blocks with mouse probability above threshold
    :param mouse_probability: Minimum mouse probability threshold
    :return: DataFrame containing the filtered motion blocks
    """
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
    return read_df_from_database(database, stmt)

def vacuum_database(database: str) -> Result:
    """
    This function performs a VACUUM operation on the database.
    """
    result = lock_database()
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
        release_database()

def get_cat_name_rfid_dict(database: str):
    stmt = "SELECT rfid, name FROM cats"
    df_cats = read_df_from_database(database, stmt)
    
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

def get_cat_names_list(database: str):
    """
    This function returns a list of cat names from the database.
    """
    stmt = "SELECT name FROM cats"
    df_cats = read_df_from_database(database, stmt)
    return df_cats['name'].tolist() if not df_cats.empty else []

def get_cat_thumbnail(database_path, cat_id, size=(32, 32)):
    """
    Returns a base64-encoded thumbnail for the cat with the given ID using cv2.
    If no image is available, returns None.
    Uses in-memory cache to avoid redundant encoding.
    """
    cache_key = (cat_id, size)
    if cache_key in _cat_thumbnail_cache:
        return _cat_thumbnail_cache[cache_key]
    try:
        # Fetch the cat image from the database
        df = db_get_cats(database_path, ReturnDataCatDB.all)
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

def check_if_table_exists(database: str, table: str) -> bool:
    """
    This function checks if the given table exists in the database.
    """
    if not os.path.exists(database):
        return False
    
    result = lock_database()
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
        release_database()

def check_if_column_exists(database: str, table: str, column: str) -> bool:
    """
    This function checks if the given column exists in the table of the database.
    """
    if not os.path.exists(database):
        return False
    
    result = lock_database()
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
        release_database()

def add_column_to_table(database: str, table: str, column: str, column_type: str) -> Result:
    """
    This function adds a new column to the table of the database.
    """
    if not os.path.exists(database):
        return Result(False, f"[DATABASE] Database '{database}' does not exist.")
    
    result = lock_database()
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
        release_database()
    
def migrate_photos_to_events(database: str) -> Result:    
    """
    Migrates all records from the 'photo' table to the 'events' table.
    NOTE: the 'photo' table is deprecated and was only used in older versions of kittyhack (<= v1.1.x), which relied on co-existence of the kittyflap service.
    """
    migrated_photos = 0
    result = lock_database()
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
        release_database()

def clear_original_kittyflap_database(database: str) -> Result:
    """
    This function clears the 'photo' and 'kportal_request' tables in the database.
    """
    result = lock_database()
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
        release_database()

def check_database_integrity(database: str, skip_lock: bool = False) -> Result:
    """
    This function checks the integrity of the database.
    :param skip_lock: If True, skip acquiring the database lock.
    """
    if not skip_lock:
        result = lock_database()
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
            release_database()
        
def backup_database_sqlite(database: str, destination_path: str) -> Result:
    """
    Perform a simple SQLite backup using the built-in backup API.
    Creates destination_path and writes a consistent snapshot of `database` into it.
    """
    try:
        # Use short lock window to avoid long blocking
        lock_res = lock_database()
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
            release_database()

        logging.info(f"[DATABASE_BACKUP] Backup written to '{destination_path}'")
        return Result(True, "")
    except Exception as e:
        logging.error(f"[DATABASE_BACKUP] Unexpected error: {e}")
        return Result(False, "backup_failed")
    

def cleanup_deleted_events(database: str) -> Result:
    """
    Removes all deleted events from the database up to the first non-deleted event.
    At least one non-deleted event will always remain in the database.
    """
    result = lock_database()
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
        release_database()

def get_ids_with_original_blob(database: str, include_deleted: bool = False) -> List[int]:
    """
    Fast retrieval of event IDs where original_image IS NOT NULL.
    Avoids loading blobs and Pandas overhead.
    """
    where = "original_image IS NOT NULL"
    if not include_deleted and check_if_column_exists(database, "events", "deleted"):
        where += " AND deleted != 1"

    result = lock_database()
    if not result.success:
        logging.error(f"[DATABASE] Failed to lock DB for get_ids_with_original_blob: {result.message}")
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
        release_database()
    return ids

def perform_event_image_migration_ids(database: str,
                                      ids: List[int],
                                      progress_fn=None,
                                      chunk_size: int = 1000,
                                      batch_size: int = 200) -> Result:
    """
    Migrate legacy BLOB images (original_image / thumbnail) to filesystem for a specific list of event IDs.
    Steps per ID:
      1. Validate row exists.
      2. Validate BLOB(s) still present (not already migrated).
      3. Write file(s) if missing, then NULL the corresponding BLOB column(s).
    Skips IDs that are missing or already migrated.

    Returns Result with summary message.
    """
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
        """Apply batched NULL updates under a short lock/transaction."""
        nonlocal pending_null_original, pending_null_thumbnail
        if not pending_null_original and not pending_null_thumbnail:
            return
        lock_res = lock_database()
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
            release_database()
            pending_null_original.clear()
            pending_null_thumbnail.clear()

    # 2) Migration pass: write files unlocked, queue DB updates
    for rid in target_ids:
        if rid not in rows_cache:
            continue
        orig_blob, thumb_blob = rows_cache[rid]

        # Original image -> filesystem
        if orig_blob is not None:
            orig_path = _original_image_path(rid)
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
            thumb_path = _thumbnail_image_path(rid)
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

