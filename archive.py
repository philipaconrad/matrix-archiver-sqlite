# Matrix archiver script.
# Copyright (c) Philip Conrad, 2020. All rights reserved.
# Released under the MIT License (See LICENSE)

# Some portions (namely event retrieval as a batching generator) are taken
# from the MIT Licensed "matrix-archive" project by Oliver Steele.
import os
import sys
import argparse
import json
import sqlite3
from datetime import datetime
from itertools import islice

from matrix_client.client import MatrixClient
import requests

from pony.orm import *


# ----------------------------------------------------------------------------
# Globals
# ----------------------------------------------------------------------------
MATRIX_USER = os.environ['MATRIX_USER']
MATRIX_PASSWORD = os.environ['MATRIX_PASSWORD']
MATRIX_HOST = os.environ.get('MATRIX_HOST', "https://matrix.org")
MATRIX_ROOM_IDS = os.environ['MATRIX_ROOM_IDS'].split(',')
EXCLUDED_ROOM_IDS = os.environ.get('EXCLUDED_MATRIX_ROOM_IDS')
if EXCLUDED_ROOM_IDS is None:
    EXCLUDED_ROOM_IDS = []
else:
    EXCLUDED_ROOM_IDS = EXCLUDED_ROOM_IDS.split(',')
MAX_FILESIZE = os.environ.get('MAX_FILESIZE', 1099511627776)  # 1 TB max filesize.


# ----------------------------------------------------------------------------
# DB Models
# ----------------------------------------------------------------------------
db = Database()

class Room(db.Entity):
    id = PrimaryKey(int, auto=True)
    room_id = Required(str, unique=True)
    display_name = Required(str)
    topic = Optional(str, nullable=True)
    members = Set('Member')
    events = Set('Event')
    retrieval_ts = Required(datetime, default=lambda: datetime.utcnow())

class Member(db.Entity):
    id = PrimaryKey(int, auto=True)
    room = Required(Room)
    display_name = Required(str)
    user_id = Required(str)
    room_id = Required(str)
    avatar_url = Optional(str, nullable=True)
    retrieval_ts = Required(datetime, default=lambda: datetime.utcnow())

class Device(db.Entity):
    id = PrimaryKey(int, auto=True)
    user_id = Required(str)
    device_id = Required(str, unique=True)
    display_name = Optional(str, nullable=True)
    last_seen_ts = Optional(str, nullable=True)
    last_seen_ip = Optional(str, nullable=True)
    retrieval_ts = Required(datetime, default=lambda: datetime.utcnow())

class Event(db.Entity):
    id = PrimaryKey(int, auto=True)
    room = Required(Room)
    content = Required(Json)
    sender = Required(str)
    type = Required(str)
    event_id = Required(str, unique=True)
    room_id = Required(str)
    origin_server_ts = Required(datetime)
    raw_json = Required(Json)
    retrieval_ts = Required(datetime, default=lambda: datetime.utcnow())

class File(db.Entity):
    id = PrimaryKey(int, auto=True)
    filename = Required(str)
    size = Required(int) # Size of file in bytes.
    mime_type = Optional(str, nullable=True)
    is_image = Required(bool, default=False) # Flag to make queries easier.
    is_cached = Required(bool, default=False) # Flag to make queries easier.
    data = Optional(bytes, nullable=True)
    fetch_url_http = Required(str, unique=True) # Resolved HTTP URL for the file.
    fetch_url_matrix = Required(str, unique=True)
    last_fetch_status = Required(str)
    last_fetch_ts = Required(datetime, default=lambda: datetime.utcnow())
    retrieval_ts = Required(datetime, default=lambda: datetime.utcnow())


# ----------------------------------------------------------------------------
# ORM Startup jazz
# ----------------------------------------------------------------------------
# Default setting. Useful for testing.
db_provider = os.environ.get('DB_PROVIDER', 'sqlite')

# Avoid running configuration stuff when generating Sphinx docs.
# Cite: https://stackoverflow.com/a/45441490
if 'sphinx' not in sys.modules:
    if db_provider == "postgres":
        # Cite: https://stackoverflow.com/a/23331896
        pwd = os.environ.get('DB_PASSWORD')
        port = os.environ.get('DB_PORT')

        # Connect to DB and auto-gen tables as needed.
        db.bind(provider='postgres',
                user=os.environ['DB_USER'],
                password=pwd,
                host=os.environ['DB_HOST'],
                port=port,
                database=os.environ['DB_NAME'])
        db.generate_mapping(create_tables=True)
        print("Connected to database: {}".format(os.environ['DB_NAME']))
    elif db_provider == "sqlite":
        # Connect to DB and auto-gen tables as needed.
        db.bind(provider='sqlite',
                filename='db.sqlite',
                create_db=True)
        db.generate_mapping(create_tables=True)
        print("Connected to database: {}".format('db.sqlite'))


# Borrowed straight from osteele/matrix-archive.
def get_room_events(client, room_id):
    """Iterate room events, starting at the cursor."""
    room = client.get_rooms()[room_id]
    print(f" |---- Reading events from room {room.display_name!r}…")
    yield from room.events
    batch_size = 1000  # empirically, this is the largest honored value
    prev_batch = room.prev_batch
    while True:
        res = room.client.api.get_room_messages(room.room_id, prev_batch, 'b',
                                                limit=batch_size)
        events = res['chunk']
        if not events:
            break
        print(f" |---- Read {len(events)} events...")
        yield from events
        prev_batch = res['end']


# Convert matrix timestamps to ISO8601 timestamps at highest resolution.
def convert_to_iso8601(ts):
    return datetime.utcfromtimestamp(ts/1000).isoformat(timespec='milliseconds')


@db_session
def add_devices(devices):
    print("Archiving Device list for user.")
    for d in devices["devices"]:
        user_id = d["user_id"]
        device_id = d["device_id"]
        display_name = d["display_name"]
        last_seen_ts = d["last_seen_ts"]
        last_seen_ip = d["last_seen_ip"]
        item = Device.get(user_id=d["user_id"], device_id=d["device_id"])
        if item is None:
            # Fix up timestamp if it is present.
            if last_seen_ts is not None:
                last_seen_ts = convert_to_iso8601(last_seen_ts)
            item = Device(user_id=user_id,
                          device_id=device_id,
                          display_name=display_name,
                          last_seen_ts=last_seen_ts,
                          last_seen_ip=last_seen_ip)
            item.flush()
        else:
            # We've seen this device before.
            print(" |-- Skipping Device: '{}' (Device ID: '{}') because it has already been archived.".format(display_name, device_id))
    commit()


@db_session
def add_rooms(rooms):
    # ------------------------------------------------
    # Back up room metadata first, then members, then events.
    for room_id in rooms:
        room = rooms[room_id]
        display_name = room.display_name
        print("Archiving Room: '{}' (Room ID: '{}')".format(display_name, room_id))
        # Skip rooms the user specifically wants to exclude.
        if room_id in EXCLUDED_ROOM_IDS:
            print(" |-- Skipping Room: '{}' (Room ID: '{}') because it is on the EXCLUDED list.".format(room.display_name, room_id))
            continue
        # Topic retrieval can fail with a 404 sometimes.
        try:
            topic = json.dumps(client.api.get_room_topic(room_id))
        except Exception as e:
            topic = None

        # See if the room already exists in the DB.
        print(" | Backing up room metadata...")
        r = Room.get(room_id=room_id)
        if r is None:
            # Room hasn't been archived before.
            item = Room(room_id=room_id,
                        display_name=display_name,
                        topic=topic)
            item.flush()
            r = item
        else:
            # We've seen this room before.
            print(" |-- Skipping metadata for Room: '{}' (Room ID: '{}') because it has already been archived.".format(display_name, room_id))

        # --------------------------------------------
        # Back up room members.
        print(" | Backing up list of room members...")
        for member in room.get_joined_members():
            display_name = member.displayname
            user_id = member.user_id
            avatar_url = member.get_avatar_url()
            # See if the member already exists in the DB.
            item = Member.get(room=r, user_id=user_id)
            if item is None:
                # Member hasn't been archived before.
                item = Member(room=r,
                              user_id=user_id,
                              room_id=r.room_id,
                              display_name=display_name,
                              avatar_url=avatar_url)
                item.flush()
            else:
                # We've seen this room before.
                print(" |-- Skipping Member: '{}' (User ID: '{}') because it has already been archived.".format(display_name, user_id))

        # --------------------------------------------
        # Back up room events.
        print(" | Backing up list of room events...")
        events = get_room_events(client, room_id)
        last_events = select(e for e in Event
                             if e.room == r).order_by(desc(Event.origin_server_ts))[:1000]
        last_event_ids = set()
        if last_events is None or last_events == []:
            # No existing backup. Let's make a new one.
            print(" |-- No existing events backup for this room. Creating a new one...")
        else:
            # We've got an existing backup, let's add to it.
            print(" |-- Checking to see if new events have occurred since the last backup...")
            last_event_ids = set([e.event_id for e in last_events])
            #print("Last event ID: {} timestamp: {}".format(last_event_id, last_event.origin_server_ts))
        new_events_saved = 0
        # Events will be pulled down in batches.
        # Note: Insertion order will be off globally, but correct within a batch.
        #   Users will need to ORDER BY `origin_server_ts` to get a globally correct ordering.
        stop_on_this_batch = False
        event_batch = list(islice(events, 0, 1000))
        while len(event_batch) > 0:
            incoming_event_ids = set([e["event_id"] for e in event_batch])
            # Set difference of incoming versus last 1k events in DB.
            diff = incoming_event_ids.difference(last_event_ids)
            for event in event_batch:
                event_id = event["event_id"]
                origin_server_ts = datetime.utcfromtimestamp(event["origin_server_ts"]/1000).isoformat(timespec='milliseconds')
                #print("Current event ID: {} timestamp: {}".format(event_id, origin_server_ts))
                # If we run into something we've already archived we'll be done after this batch.
                if event_id not in diff:
                    stop_on_this_batch = True
                    continue
                # Otherwise, archive this event.
                new_events_saved += 1
                content = event["content"]
                sender = event["sender"]
                type = event["type"]
                origin_server_ts = datetime.utcfromtimestamp(event["origin_server_ts"]/1000).isoformat(timespec='milliseconds')
                raw_json = json.dumps(event)

                item = Event(room=r,
                             event_id=event_id,
                             room_id=r.room_id,
                             content=content,
                             sender=sender,
                             type=type,
                             origin_server_ts=origin_server_ts,
                             raw_json=raw_json)
                item.flush()

                # Download files if message.content['msgtype'] == 'm.file'
                if "msgtype" in item.content.keys() and item.content["msgtype"] in ["m.file", "m.image"]:
                    print(" |---- Attempting to archive file: '{}'".format(item.content["body"]))
                    filename = item.content["body"]
                    file_size = item.content["info"]["size"]
                    is_image = (item.content["msgtype"] == "m.image")
                    matrix_download_url = item.content["url"]
                    http_download_url = client.api.get_download_url(matrix_download_url)
                    data = None
                    is_cached = False
                    last_fetch_status = "Fail"

                    file_entry = File.get(fetch_url_matrix=matrix_download_url)
                    # If not cached, or last fetch failed, try fetching the file.
                    if file_entry is None or file_entry.is_cached == False:
                        try:
                            req = requests.get(http_download_url, stream=True)
                            if int(req.headers["content-length"]) < MAX_FILESIZE:
                                data = req.content
                                is_cached = True
                                last_fetch_status = "{} {}".format(req.status_code, req.reason)
                            else:
                                print(" |     File: '{}' of size {} bytes was not archived due to size in excess of limit ({} bytes).".format(filename, file_size, MAX_FILESIZE))
                        except Exception as e:
                            print("       Could not fetch file. Traceback:\n       {}".format(e))
                            is_cached = False
                    else:
                        print(" |------ Skipping because file is already archived!")

                    if file_entry is None:
                        file_entry = File(filename=filename,
                                          size=file_size,
                                          mime_type=item.content["info"].get("mimetype"),
                                          is_image=is_image,
                                          is_cached=is_cached,
                                          data=data,
                                          fetch_url_http=http_download_url,
                                          fetch_url_matrix=matrix_download_url,
                                          last_fetch_status=last_fetch_status)
                    else:
                        # Update data field if we had a successful fetch.
                        if data is not None:
                            file_entry.data = data
                        file_entry.last_fetch_status = last_fetch_status
                        file_entry.last_fetch_ts = datetime.utcnow().isoformat()
                    file_entry.flush()

            # Terminate if we hit known event IDs in this batch.
            if stop_on_this_batch:
                break
            # Fetch next batch.
            event_batch = list(islice(events, 0, 1000))
            commit()
        commit()
        print(" | Archived {} new events for room '{}'".format(new_events_saved, room.display_name))


# ----------------------------------------------------------------------------
# Main function
# ----------------------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Matrix Room Archiver Client')
    parser.add_argument('-u', '--user', type=str, help="Username to use for logging in.")
    parser.add_argument('-p', '--password', type=str, help="Password to use for logging in.")
    parser.add_argument('--db', type=str, default="archive.sqlite", help="Name of the database file to export to. (default: 'archive.sqlite')")
    parser.add_argument('--room', action="append", help="Name of the Matrix room to export. Applying this argument multiple times will export multiple rooms, in sequence.")
    parser.add_argument('--host', type=str, help="Matrix host address. (default: 'https://matrix.org')")
    args = parser.parse_args()

    matrix_user = args.user
    matrix_password = args.password
    matrix_rooms = args.room
    matrix_host = args.host
    dbname = args.db

    print("MATRIX_HOST: '{}'".format(MATRIX_HOST))
    print("MATRIX_USER: '{}'".format(MATRIX_USER))
    if MATRIX_PASSWORD is not None:
        print("MATRIX_PASSWORD: '{}'".format("".join(["*" for c in MATRIX_PASSWORD])))
    else:
        print("MATRIX_PASSWORD: '{}'".format(MATRIX_PASSWORD))
    print("MATRIX_ROOM_IDS: '{}'".format(MATRIX_ROOM_IDS))

    print("Signing into {}...".format(MATRIX_HOST))
    client = MatrixClient(MATRIX_HOST)
    token = client.login(username=MATRIX_USER, password=MATRIX_PASSWORD, device_id="Matrix Archiver")
    #print("Token: {}".format(token))

    # Archive the devices for this user.
    add_devices(client.api.get_devices())

    # Archive the rooms.
    add_rooms(client.get_rooms())

    print("Done with archiving run. Logging out of Matrix...")
    client.logout()

