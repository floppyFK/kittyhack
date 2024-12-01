from dataclasses import dataclass
import pandas as pd
import sqlite3
from enum import Enum
from threading import Lock
import logging
import sys
import time as tm
from src.helper import *

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

# Return types for cat database actions
class ReturnDataCatDB(Enum):
    all = 0
    all_except_photos = 1

# Return types for config database actions
class ReturnDataConfigDB(Enum):
    all = 0
    all_except_password = 1

# Lock for database writes
db_write_lock = Lock()

def lock_database(timeout: int = 30, check_interval: float = 0.1) -> Result:
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
            logging.info("Database lock acquired.")
            return Result(True, None)
        tm.sleep(check_interval)
    error_message = f"Database lock not released within the given timeout ({timeout}s)."
    logging.error(error_message)
    return Result(False, error_message)

def release_database():
    """
    This function releases the database lock (db_write_lock) after a write operation is done.
    """
    if db_write_lock.locked():
        db_write_lock.release()
        logging.info("Database lock released.")
    else:
        logging.warning("Database lock is not acquired. Nothing to release.")

###### General database operations ######

def read_df_from_database(database: str, stmt: str) -> pd.DataFrame:
    try:
        conn = sqlite3.connect(database, timeout=30)
        df = pd.read_sql_query(stmt, conn)
        conn.close()
    except Exception as e:
        logging.error(f"Failed to read from database '{database}': {e}")
        df = pd.DataFrame.empty
    else:
        logging.debug(f"Read from database '{database}': {df}")
        
    return df

def read_column_info_from_database(database: str, table: str):
    try:
        conn = sqlite3.connect(database, timeout=30)
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table})")
        columns_info = cursor.fetchall()
        conn.close()
    except Exception as e:
        logging.error(f"Failed to read column information from database '{database}': {e}")
        columns_info = []
    else:
        logging.debug(f"Read column information from database '{database}': {columns_info}")

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
        error_message = f"An error occurred while updating the database '{database}': {e}"
        logging.error(error_message)
        return Result(False, error_message)
    else:
        # success
        logging.debug(f"Successfully executed statement to database '{database}': {stmt}")
        return Result(True, None)
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
    this function returns all dataframes from the 'photo' table, based on the
    specified filter.
    If no filters are specified, this function returns all avaliable dataframes.
    The newest data are at the top of the dataframe.
    """
    if return_data == ReturnDataPhotosDB.all:
         columns = "id, created_at, blob_picture, no_mouse_probability, mouse_probability, kittyflap_id, cat_id, rfid, false_accept_probability"
    elif return_data == ReturnDataPhotosDB.all_except_photos:
         columns = "id, created_at, no_mouse_probability, mouse_probability, kittyflap_id, cat_id, rfid, false_accept_probability"
    elif return_data == ReturnDataPhotosDB.only_ids:
         columns = "id"
    else:
         columns = "*"
    
    # Check if 'deleted' column exists (this column exists only in the kittyhack database)
    columns_info = read_column_info_from_database(database, "photo")
    column_names = [info[1] for info in columns_info]
    if 'deleted' in column_names and ignore_deleted == True:
        stmt = f"SELECT {columns} FROM photo WHERE created_at BETWEEN '{date_start}' AND '{date_end}' AND deleted != 1"
    else:
        stmt = f"SELECT {columns} FROM photo WHERE created_at BETWEEN '{date_start}' AND '{date_end}'"
    if mouse_only:
        stmt = f"{stmt} AND mouse_probability >= {mouse_probability}"
    if cats_only:
        stmt = f"{stmt} AND rfid != ''"
    # reverse the row order, based on column 'id', so that the newest photos are at the top
    stmt = f"{stmt} ORDER BY id DESC"

    if elements_per_page != sys.maxsize:
        # calculate the offset for the current page
        offset = page_index * elements_per_page
        stmt = f"{stmt} LIMIT {elements_per_page} OFFSET {offset}"

    return read_df_from_database(database, stmt)

def db_get_cats(database: str, return_data: ReturnDataCatDB):
    """
    this function returns all dataframes from the 'cat' table.
    """
    if return_data == ReturnDataCatDB.all:
         columns = "*"
    elif return_data == ReturnDataCatDB.all_except_photos:
        columns = "id, created_at, kittyflap_id, name, rfid, cat_config_id"

    stmt = f"SELECT {columns} FROM cat"
    return read_df_from_database(database, stmt)

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
    logging.info(f"Writing new kittyflap configuration to 'config' table in database '{database}': {data}")
    stmt = f"UPDATE config SET {data} WHERE id = (SELECT id FROM config LIMIT 1)"
    result = write_stmt_to_database(database, stmt)
    if result.success == True:
        logging.info("Kittyflap configuration updated successfully.")
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
        return Result(True, None)
    finally:
        release_database()

def db_duplicate_photos(src_database: str, dst_database: str, dst_max_photos: int):
    """
    This function duplicates the photos from one database to another.
    If the number of photos in the destination database exceeds dst_max_photos,
    the oldest photos will be deleted.
    Dataframes with the value 'deleted = 1' will not be considered.
    """
    src_photos = db_get_photos(src_database, ReturnDataPhotosDB.only_ids, ignore_deleted=False)
    logging.info("Reading photos from source database done.")
    dst_photos = db_get_photos(dst_database, ReturnDataPhotosDB.only_ids, ignore_deleted=False)
    logging.info("Reading photos from destination database done.")

    src_ids = src_photos['id'].tolist() if not src_photos.empty else []
    dst_ids = dst_photos['id'].tolist() if not dst_photos.empty else []

    new_ids = set(src_ids) - set(dst_ids)

    if not new_ids:
        logging.info("No new photos to add to the destination database.")
        return Result(True, None)
    
    logging.info(f"Number of new photos to be added to the destination database: {len(new_ids)}")

    result = lock_database()
    if not result.success:
        return result

    try:
        conn_dst = sqlite3.connect(dst_database, timeout=30)
        cursor_dst = conn_dst.cursor()

        for photo_id in new_ids:
            photo_df = read_photo_by_id(src_database, photo_id)
            if not photo_df.empty:
                logging.info(f"Adding photo with ID '{photo_id}' to the destination database.")
                photo_df['deleted'] = 0
                columns = ', '.join(photo_df.columns)
                values = ', '.join(['?' for _ in photo_df.columns])
                values_list = [
                    int(photo_df.iloc[0]['id']),
                    str(photo_df.iloc[0]['created_at']),
                    photo_df.iloc[0]['blob_picture'],
                    float(photo_df.iloc[0]['no_mouse_probability']),
                    float(photo_df.iloc[0]['mouse_probability']),
                    int(photo_df.iloc[0]['kittyflap_id']),
                    int(photo_df.iloc[0]['cat_id']),
                    str(photo_df.iloc[0]['rfid']),
                    float(photo_df.iloc[0]['false_accept_probability']),
                    int(photo_df.iloc[0]['deleted'])
                ]
                cursor_dst.execute(f"INSERT INTO photo ({columns}) VALUES ({values})", values_list)

        conn_dst.commit()

        # Check if the number of photos exceeds dst_max_photos and delete the oldest if necessary
        cursor_dst.execute("SELECT COUNT(*) FROM photo WHERE deleted != 1")
        total_photos = cursor_dst.fetchone()[0]

        if total_photos > dst_max_photos:
            excess_photos = total_photos - dst_max_photos
            logging.info(f"Number of photos in the destination database exceeds the limit. Deleting {excess_photos} oldest photos.")
            cursor_dst.execute(f"DELETE FROM photo WHERE id IN (SELECT id FROM photo WHERE deleted != 1 ORDER BY created_at ASC LIMIT {excess_photos})")

        conn_dst.commit()
    except Exception as e:
        error_message = f"An error occurred while duplicating photos to the database '{dst_database}': {e}"
        logging.error(error_message)
        return Result(False, error_message)
    else:
        logging.info(f"Successfully duplicated photos to the database '{dst_database}'.")
        return Result(True, None)
    finally:
        release_database()

def read_photo_by_id(database: str, photo_id: int) -> pd.DataFrame:
    """
    This function reads a specific dataframe based on the ID from the source database.
    """
    columns = "id, created_at, blob_picture, no_mouse_probability, mouse_probability, kittyflap_id, cat_id, rfid, false_accept_probability"
    stmt = f"SELECT {columns} FROM photo WHERE id = {photo_id}"
    return read_df_from_database(database, stmt)

def delete_photo_by_id(database: str, photo_id: int) -> Result:
    """
    This function deletes a specific dataframe based on the ID from the source database.
    """
    stmt = f"UPDATE photo SET blob_picture = NULL, deleted = 1 WHERE id = {photo_id}"
    result = write_stmt_to_database(database, stmt)
    if result.success == True:
        logging.info(f"Photo with ID '{photo_id}' deleted successfully.")
    return result