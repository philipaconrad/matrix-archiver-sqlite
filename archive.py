# Matrix archiver script.
# Copyright (c) Philip Conrad, 2020. All rights reserved.
# Released under the MIT License (See LICENSE)

# Some portions (namely event retrieval as a batching generator) are taken
# from the MIT Licensed "matrix-archive" project by Oliver Steele.
import os
import argparse
import json
import sqlite3
from datetime import datetime
from itertools import islice

from matrix_client.client import MatrixClient

MATRIX_USER = os.environ['MATRIX_USER']
MATRIX_PASSWORD = os.environ['MATRIX_PASSWORD']
MATRIX_TOKEN = os.environ['MATRIX_TOKEN']
MATRIX_HOST = os.environ.get('MATRIX_HOST', "https://matrix.org")
MATRIX_ROOM_IDS = os.environ['MATRIX_ROOM_IDS'].split(',')
EXCLUDED_ROOM_IDS = os.environ.get('EXCLUDED_MATRIX_ROOM_IDS')
if EXCLUDED_ROOM_IDS is None:
    EXCLUDED_ROOM_IDS = []
else:
    EXCLUDED_ROOM_IDS = EXCLUDED_ROOM_IDS.split(',')

# Borrowed straight from osteele/matrix-archive.
def get_room_events(client, room_id):
    """Iterate room events, starting at the cursor."""
    room = client.get_rooms()[room_id]
    print(f"Reading events from room {room.display_name!r}…")
    yield from room.events
    batch_size = 1000  # empirically, this is the largest honored value
    prev_batch = room.prev_batch
    while True:
        res = room.client.api.get_room_messages(room.room_id, prev_batch, 'b',
                                                limit=batch_size)
        events = res['chunk']
        if not events:
            break
        print(f"Read {len(events)} events...")
        yield from events
        prev_batch = res['end']


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
    print("MATRIX_PASSWORD: '{}'".format(MATRIX_PASSWORD))
    print("MATRIX_TOKEN: '{}'".format(MATRIX_TOKEN))
    print("MATRIX_ROOM_IDS: '{}'".format(MATRIX_ROOM_IDS))

    conn = sqlite3.connect(dbname)
    c = conn.cursor()
    c.executescript("""
CREATE TABLE IF NOT EXISTS devices (
	id      INTEGER PRIMARY KEY AUTOINCREMENT,
	member_id	TEXT NOT NULL,
	data		TEXT NOT NULL,
	retrieval_ts	TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
	id      INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id TEXT NOT NULL,
	data	TEXT NOT NULL,
	retrieval_ts	TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
	id      	INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id 	TEXT NOT NULL,
	display_name	TEXT NOT NULL,
	topic		TEXT NOT NULL,
	retrieval_ts	TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
	id      INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id TEXT NOT NULL,
	data	TEXT NOT NULL,
	retrieval_ts	TEXT NOT NULL
);
    """)

    print("Signing into {}...".format(MATRIX_HOST))
    client = MatrixClient(MATRIX_HOST)
    token = client.login(username=MATRIX_USER, password=MATRIX_PASSWORD, device_id="Matrix Archiver")
    #print("Token: {}".format(token))

    rooms = client.get_rooms()
    #print("Rooms:\n{}".format(rooms))

    devices = client.api.get_devices()
    #devices = json.loads(open('devices.json', 'r').read())
    #print("Devices:\n{}".format(devices))

    print("Archiving Device list for user.")
    for d in devices["devices"]:
        # Last seen TS is Unix timestamp in milliseconds since epoch.
        # print(datetime.utcfromtimestamp(ts/1000).isoformat(timespec='milliseconds'))
        #last_seen_ts = None
        #if d["last_seen_ts"] is not None:
        #    last_seen_ts = datetime.utcfromtimestamp(d["last_seen_ts"] / 1000)
        #    last_seen_ts = last_seen_ts.isoformat(timespec='milliseconds')
        c.execute("INSERT INTO devices(member_id, data, retrieval_ts) VALUES (?, ?, ?);",
                  (d["user_id"],
                   json.dumps(d),
                   datetime.utcnow().isoformat(timespec='milliseconds')))
    conn.commit()

    for room_id in rooms:
        if room_id in EXCLUDED_ROOM_IDS:
            print("Skipping Room: '{}' (Room ID: {}) because it is on the EXCLUDED list.".format(room.display_name, room_id))
            continue
        room = rooms[room_id]
        print("Archiving Room: '{}' (Room ID: '{}')".format(room.display_name, room_id))
        print(" | Backing up room metadata...")
        try:
            topic = client.api.get_room_topic(room_id)
        except Exception as e:
            topic = None
        c.execute("INSERT INTO rooms(room_id, display_name, topic, retrieval_ts) VALUES (?, ?, ?, ?);",
                  (room_id,
                   room.display_name,
                   json.dumps(topic),
                   datetime.utcnow().isoformat(timespec='milliseconds')))
        conn.commit()
        # Back up members list.
        print(" | Backing up list of room members...")
        members = [{"displayname": m.displayname,
                    "user_id": m.user_id,
                    "avatar_url": m.get_avatar_url()}
                   for m in room.get_joined_members()]
        for member in members:
            c.execute("INSERT INTO members(room_id, data, retrieval_ts) VALUES (?, ?, ?);",
                      (room_id,
                       json.dumps(member),
                       datetime.utcnow().isoformat(timespec='milliseconds')))
            conn.commit()
        # Back up events list.
        print(" | Backing up list of room events...")
        events = get_room_events(client, room_id)
        for event in events:
            c.execute("INSERT INTO events(room_id, data, retrieval_ts) VALUES (?, ?, ?);",
                      (room_id,
                       json.dumps(event),
                       datetime.utcnow().isoformat(timespec='milliseconds')))
            conn.commit()

    print("Done with archiving run. Closing database...")
    conn.close()
    client.logout()

