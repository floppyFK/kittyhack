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
from typing import TypedDict, List
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
        return pd.DataFrame.empty

    try:
        conn = sqlite3.connect(database, timeout=30)
        df = pd.read_sql_query(stmt, conn)
        conn.close()
    except Exception as e:
        logging.error(f"[DATABASE] Failed to read from database '{database}': {e}")
        df = pd.DataFrame.empty
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

    return read_df_from_database(database, stmt)

def db_get_photos_by_block_id(database: str, block_id: int, return_data: ReturnDataPhotosDB = ReturnDataPhotosDB.all):
    """
    this function returns all dataframes from the 'events' table, based on the
    specified block_id.
    
    :param database: Path to the database file
    :param block_id: ID of the block to retrieve
    :param return_data: Type of data to return (ReturnDataPhotosDB enum)
    :return: DataFrame containing the requested data
    """
    if return_data == ReturnDataPhotosDB.all:
        columns = "id, block_id, created_at, event_type, original_image, modified_image, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text, thumbnail"
    elif return_data == ReturnDataPhotosDB.all_modified_image:
        columns = "id, block_id, created_at, event_type, modified_image, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text, thumbnail"
    elif return_data == ReturnDataPhotosDB.all_original_image:
        columns = "id, block_id, created_at, event_type, original_image, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text, thumbnail"
    elif return_data == ReturnDataPhotosDB.all_except_photos:
        columns = "id, block_id, created_at, event_type, no_mouse_probability, mouse_probability, own_cat_probability, rfid, event_text"
    elif return_data == ReturnDataPhotosDB.only_ids:
        columns = "id"
        
    stmt = f"SELECT {columns} FROM events WHERE block_id = {block_id}"
    return read_df_from_database(database, stmt)

def get_ids_without_thumbnail(database: str):
    """
    This function returns the IDs of the images that do not have a thumbnail.
    
    :param database: Path to the database file
    :return: List of IDs without thumbnails
    """
    stmt = "SELECT id FROM events WHERE thumbnail IS NULL AND deleted != 1"
    df = read_df_from_database(database, stmt)
    return df['id'].tolist() if not df.empty else []

def get_thubmnail_by_id(database: str, photo_id: int):
    """
    This function reads a specific thumbnail image based on the ID from the source database.
    If no thumbnail exists, it creates one from the original image.
    """
    stmt = f"SELECT thumbnail, original_image FROM events WHERE id = {photo_id}"
    df = read_df_from_database(database, stmt)
    
    if df.empty:
        logging.error(f"[DATABASE] Photo with ID {photo_id} not found")
        return None
        
    # Check if thumbnail exists
    if df.iloc[0]['thumbnail'] is not None:
        return df.iloc[0]['thumbnail']
    
    # If no thumbnail exists, create one from the original image
    original_image = df.iloc[0]['original_image']
    if original_image is None:
        logging.error(f"[DATABASE] Original image not found for photo ID {photo_id}")
        return None
        
    try:
        # Create thumbnail
        thumbnail = process_image(original_image, 640, 480, 50)
        
        # Store thumbnail in database
        result = lock_database()
        if not result.success:
            logging.error(f"[DATABASE] Failed to acquire lock to update thumbnail: {result.message}")
            return thumbnail
            
        try:
            conn = sqlite3.connect(database, timeout=30)
            cursor = conn.cursor()
            cursor.execute("UPDATE events SET thumbnail = ? WHERE id = ?", (thumbnail, photo_id))
            conn.commit()
            conn.close()
            logging.info(f"[DATABASE] Created and stored thumbnail for photo ID {photo_id}")
        except Exception as e:
            logging.error(f"[DATABASE] Failed to store thumbnail in database: {e}")
        finally:
            release_database()
            
        return thumbnail
    except Exception as e:
        logging.error(f"[DATABASE] Failed to create thumbnail from original image: {e}")
        return None

def db_get_cats(database: str, return_data: ReturnDataCatDB):
    """
    this function returns all dataframes from the 'cats' table.
    """
    if return_data == ReturnDataCatDB.all:
         columns = "*"
    elif return_data == ReturnDataCatDB.all_except_photos:
        columns = "id, created_at, name, rfid"

    stmt = f"SELECT {columns} FROM cats"
    return read_df_from_database(database, stmt)

def db_get_all_rfid_tags(database: str):
    """
    This function returns all RFID tags from the 'cats' table as an array.
    """
    stmt = "SELECT rfid FROM cats"
    df = read_df_from_database(database, stmt)
    if df.empty:
        return []
    return df['rfid'].tolist()

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
            cat_image BLOB
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

def db_update_cat_data_by_id(database: str, cat_id: int, name: str, rfid: str, cat_image_path: str) -> Result:
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
            cursor.execute("UPDATE cats SET name = ?, rfid = ? WHERE id = ?", (name, rfid, cat_id))
        else:
            cursor.execute("UPDATE cats SET name = ?, rfid = ?, cat_image = ? WHERE id = ?", (name, rfid, cat_image_blob, cat_id))
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

def db_add_new_cat(database: str, name: str, rfid: str, cat_image_path: str) -> Result: 
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
        cursor.execute("INSERT INTO cats (id, created_at, name, rfid, cat_image) VALUES (?, ?, ?, ?, ?)", (id, get_utc_date_string(tm.time()), name, rfid, cat_image_blob))
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

def read_photo_by_id(database: str, photo_id: int) -> pd.DataFrame:
    """
    This function reads a specific dataframe based on the ID from the source database.
    """
    columns = "id, block_id, created_at, event_type, original_image, modified_image, mouse_probability, no_mouse_probability, own_cat_probability, rfid, event_text"
    stmt = f"SELECT {columns} FROM events WHERE id = {photo_id}"
    return read_df_from_database(database, stmt)

def delete_photo_by_id(database: str, photo_id: int) -> Result:
    """
    This function deletes a specific dataframe based on the ID from the source database.
    """
    stmt = f"UPDATE events SET original_image = NULL, modified_image = NULL, thumbnail = NULL, deleted = 1 WHERE id = {photo_id}"
    result = write_stmt_to_database(database, stmt)
    if result.success == True:
        logging.info(f"[DATABASE] Photo with ID '{photo_id}' deleted successfully.")
    return result

def delete_photos_by_block_id(database: str, block_id: int) -> Result:
    """
    This function deletes all dataframes based on the block_id from the source database.
    """
    stmt = f"UPDATE events SET original_image = NULL, modified_image = NULL, thumbnail = NULL, deleted = 1 WHERE block_id = {block_id}"
    result = write_stmt_to_database(database, stmt)
    if result.success == True:
        logging.info(f"[DATABASE] Photos with block ID '{block_id}' deleted successfully.")
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
                columns = "block_id, created_at, event_type, original_image, modified_image, mouse_probability, no_mouse_probability, own_cat_probability, rfid, event_text"
                values = ', '.join(['?' for _ in columns.split(', ')])
                values_list = [
                    db_block_id,
                    get_utc_date_string(element.timestamp),
                    event_type,
                    None if element.original_image is None else element.original_image,
                    None if element.modified_image is None else element.modified_image,
                    element.mouse_probability,
                    element.no_mouse_probability,
                    element.own_cat_probability,
                    element.tag_id,
                    event_json
                ]
                cursor.execute(f"INSERT INTO events ({columns}) VALUES ({values})", values_list)
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
            cursor.execute(f"SELECT id, created_at FROM events WHERE deleted != 1 ORDER BY created_at ASC LIMIT {excess_photos}")
            photos_to_delete = cursor.fetchall()
            for photo in photos_to_delete:
                logging.info(f"[DATABASE] Deleting photo ID: {photo[0]}, created_at: {photo[1]}")
                cursor.execute(f"UPDATE events SET deleted = 1, original_image = NULL, modified_image = NULL, thumbnail = NULL WHERE id = {photo[0]}")

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
    return dict(zip(df_cats['rfid'], df_cats['name']))

def get_cat_names_list(database: str):
    """
    This function returns a list of cat names from the database.
    """
    stmt = "SELECT name FROM cats"
    df_cats = read_df_from_database(database, stmt)
    return df_cats['name'].tolist() if not df_cats.empty else []

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
        
def backup_database(database: str, backup_path: str) -> Result:
    """
    Backup the main database file to a specified backup location.
    This function performs a database backup with various safety checks.
    Args:
        database (str): Path to the source database file
        backup_path (str): Path where the backup should be created
    Returns:
        Result: Object containing:
            - success (bool): True if backup completed successfully
            - message (str): Error message if backup failed, possible values:
                - "database_locked": Could not acquire database lock
                - "kittyhack_db_corrupted": Source database is invalid or empty
                - "disk_space_full": Insufficient disk space for backup
                - "backup_verification_failed": Backup file failed integrity check
                - "backup_failed": General backup operation failure
                - None: If backup succeeded
    """
    free_disk_space = get_free_disk_space()
    current_time = datetime.now()

    logging.info("[DATABASE_BACKUP] Starting database backup...")
    # Try to acquire database lock
    result = lock_database()
    if not result.success:
        logging.error(f"[DATABASE_BACKUP] Failed to lock database: {result.message}")
        return Result(False, "database_locked")
    
    try:
        # Check prerequisites for backup
        db_integrity = check_database_integrity(database, skip_lock=True)
        kittyhack_db_size = get_database_size()
        kittyhack_db_backup_size = get_file_size(backup_path) if os.path.exists(backup_path) else 0

        # Check database is valid
        if kittyhack_db_size == 0 or not db_integrity.success:
            logging.error("[DATABASE_BACKUP] Source database is invalid or empty")
            return Result(False, "kittyhack_db_corrupted")

        # Verify we would have enough disk space (>500MB) after backup
        required_space = kittyhack_db_size - kittyhack_db_backup_size + 500
        if free_disk_space < required_space:
            logging.error(f"[DATABASE_BACKUP] Insufficient disk space: {free_disk_space}MB free, need {required_space}MB")
            return Result(False, "disk_space_full")

        if not os.path.exists(backup_path):
            # Create a full backup if the backup file does not exist
            shutil.copy2(database, backup_path)
        else:
            # Synchronize tables from the source database to the backup database
            conn_src = sqlite3.connect(database, timeout=30)
            conn_dst = sqlite3.connect(backup_path, timeout=30)
            cursor_src = conn_src.cursor()
            cursor_dst = conn_dst.cursor()

            # Synchronize events table
            # Compare only the 'id' and 'deleted' columns in the event tables of both databases.
            cursor_src.execute("SELECT id, deleted FROM events")
            events_src = cursor_src.fetchall()
            cursor_dst.execute("SELECT id, deleted FROM events")
            events_dst = cursor_dst.fetchall()
            events_dst_dict = {row[0]: row[1] for row in events_dst}

            for row in events_src:
                src_id, src_deleted = row
                if src_id not in events_dst_dict:
                    cursor_src.execute("SELECT * FROM events WHERE id = ?", (src_id,))
                    full_row = cursor_src.fetchone()
                    cursor_dst.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", full_row)
                elif events_dst_dict[src_id] != src_deleted:
                    cursor_src.execute("SELECT * FROM events WHERE id = ?", (src_id,))
                    full_row = cursor_src.fetchone()
                    cursor_dst.execute("UPDATE events SET block_id = ?, created_at = ?, event_type = ?, original_image = ?, modified_image = ?, mouse_probability = ?, no_mouse_probability = ?, own_cat_probability = ?, rfid = ?, event_text = ?, deleted = ?, thumbnail = ? WHERE id = ?", full_row[1:] + (src_id,))

            # Delete events from backup that don't exist in source
            cursor_src.execute("SELECT id FROM events")
            events_src_ids = {row[0] for row in cursor_src.fetchall()}
            cursor_dst.execute("DELETE FROM events WHERE id NOT IN (%s)" % ','.join('?' * len(events_src_ids)), tuple(events_src_ids)) if events_src_ids else cursor_dst.execute("DELETE FROM events")

            # Synchronize cats table
            cursor_dst.execute("DELETE FROM cats")
            cursor_src.execute("SELECT * FROM cats")
            cats_src = cursor_src.fetchall()
            for row in cats_src:
                cursor_dst.execute("INSERT INTO cats VALUES (?, ?, ?, ?, ?)", row)

            conn_dst.commit()
            conn_src.close()
            conn_dst.close()

            # Verify backup integrity
            if check_database_integrity(backup_path, skip_lock=True).success:
                CONFIG['LAST_DB_BACKUP_DATE'] = current_time.strftime('%Y-%m-%d %H:%M:%S')
                update_single_config_parameter("LAST_DB_BACKUP_DATE")
                logging.info("[DATABASE_BACKUP] Backup completed successfully")
                return Result(True, "")
            else:
                logging.error("[DATABASE_BACKUP] Backup verification failed - will retry next run")
                try:
                    os.remove(backup_path)
                except Exception as e:
                    logging.error(f"[DATABASE_BACKUP] Failed to delete corrupted backup file: {e}")
                return Result(False, "backup_verification_failed")
                
    except Exception as e:
        logging.error(f"[DATABASE_BACKUP] Backup failed: {e}")
        try:
            os.remove(backup_path)
        except Exception as e:
            logging.error(f"[DATABASE_BACKUP] Failed to delete corrupted backup file: {e}")
        return Result(False, "backup_failed")
            
    finally:
        # Always release the lock
        release_database()
        duration = (datetime.now() - current_time).total_seconds()
        logging.info(f"[DATABASE_BACKUP] Database backup done within {duration} seconds")

        # Vacuum the backup database to reclaim space
        try:
            conn = sqlite3.connect(backup_path, timeout=30)
            cursor = conn.cursor()
            cursor.execute("VACUUM")
            conn.close()
            logging.info("[DATABASE_BACKUP] Successfully vacuumed backup database")
        except Exception as e:
            logging.warning(f"[DATABASE_BACKUP] Failed to vacuum backup database: {e}")
        

def restore_database_backup(database: str, backup_path: str) -> Result:
    """
    Restores a database from a backup file.
    This function attempts to restore a database from a specified backup file.
    Args:
        database (str): The path to the target database file that will be restored.
        backup_path (str): The path to the backup file that will be used for restoration.
    Returns:
        Result: A Result object containing:
            - success (bool): True if restore was successful, False otherwise
            - message (str): Error message if failed, None if successful
                Possible error messages:
                - 'database_locked': Could not acquire database lock
                - 'backup_not_found': Backup file does not exist
                - 'restore_failed': Error occurred during restore
    """
    # Try to acquire database lock
    result = lock_database()
    if not result.success:
        logging.error(f"[DATABASE_BACKUP] Failed to lock database: {result.message}")
        return Result(False, "database_locked")
    
    try:
        # Check if backup file exists
        if not os.path.exists(backup_path):
            logging.error(f"[DATABASE_BACKUP] Backup file not found: {backup_path}")
            return Result(False, "backup_not_found")
        
        # Perform the restore
        try:
            shutil.copy2(backup_path, database)
            logging.info("[DATABASE_BACKUP] Restore completed successfully")
            return Result(True, "")
        except Exception as e:
            logging.error(f"[DATABASE_BACKUP] Restore failed: {e}")
            return Result(False, "restore_failed")
            
    finally:
        # Always release the lock
        release_database()

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
