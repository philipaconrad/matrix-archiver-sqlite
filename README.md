Matrix Archiver
---------------

This project provides a Python 3 script which will log into Matrix, and export everything it can reach, saving the results into a SQLite database (other databases may be supported in the future).

## Usage

```
export MATRIX_USER="@bob:matrix.org"
export MATRIX_PASSWORD="YourPasswordGoesHere"

python3 archive.py --db my_archive.db
```

This will save `@bob`'s Matrix chats in an SQLite DB named `my_archive.db`.

## Features

 - Archives full device list for the user.
 - Archives full event list for Matrix rooms.
 - Image and files are downloaded, along with metadata.
 - Archives full member lists for Matrix rooms.
 - Incremental backups on everything! (Very important in long-running rooms)

## Roadmap

 - Export scripts, such as exporting a room to HTML.
 - Support for other databases, like Postgre and MariaDB.

## Known issues

 - In longer chats, if a backup fails partway through backing up the events in a room (over 1k events), the incremental backup logic can prevent a full backup from occurring on the next run. To work around this, ensure the first backup gets *everything* in the room of interest, and then incremental backups should work correctly for future archiving runs. (This is planned to be fixed in a future version!)

## Inspired by

This project was inspired by the work Oliver Steele did with an "Export to MongoDB" archiver, called [matrix-archive][1].

   [1]: https://github.com/osteele/matrix-archive

## License

This project is released under the terms of the MIT License.

See the `LICENSE` file for the full license text.
