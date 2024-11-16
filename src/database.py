import pandas as pd
import sqlite3
from enum import Enum
import logging
import sys

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

def read_df_from_database(database: str, stmt: str):
    try:
        df = pd.read_sql(stmt, con=sqlite3.connect(database))
    except Exception as e:
        logging.error(f"Failed to read from database {database}: {e}")
        return None
    else:
        logging.debug(f"Read from database {database}: {df}")
        return df

def db_get_photos(database: str, return_data: ReturnDataPhotosDB, date_start="2020-01-01 00:00:00", date_end="2100-12-31 23:59:59", cats_only=False, mouse_only=False, mouse_probability=0.0, page_index = 0, elements_per_page = sys.maxsize):
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