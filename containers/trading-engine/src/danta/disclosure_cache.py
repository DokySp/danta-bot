"""Per-receipt originals; legacy cache migration never loads the whole JSON."""
import codecs
import json

from .config import canonical


def iter_records(blob):
    decoder, utf8 = json.JSONDecoder(), codecs.getincrementaldecoder('utf-8')()
    buffer, position, ended = '', 0, False

    def fill():
        nonlocal buffer, position, ended
        chunk = blob.read(1024 * 1024)
        buffer = buffer[position:] + utf8.decode(chunk, final=not chunk)
        position, ended = 0, not chunk

    def skip(characters):
        nonlocal position
        while True:
            while position < len(buffer) and buffer[position] in characters:
                position += 1
            if position < len(buffer) or ended:
                return
            fill()

    def take():
        nonlocal position
        while True:
            try:
                value, position = decoder.raw_decode(buffer, position)
                return value
            except json.JSONDecodeError:
                if ended:
                    raise
                fill()

    fill()
    skip(' \r\n\t')
    if not buffer or buffer[position] != '{':
        raise ValueError('INVALID_DISCLOSURE_CACHE')
    position += 1
    skip(' \r\n\t')
    while position < len(buffer) and buffer[position] != '}':
        key = take()
        skip(' \r\n\t')
        if not isinstance(key, str) or position >= len(buffer) or buffer[position] != ':':
            raise ValueError('INVALID_DISCLOSURE_CACHE')
        position += 1
        skip(' \r\n\t')
        yield key, take()
        skip(' \r\n\t')
        if position < len(buffer) and buffer[position] == ',':
            position += 1
            skip(' \r\n\t')
            if position < len(buffer) and buffer[position] == '}':
                raise ValueError('INVALID_DISCLOSURE_CACHE')
        elif position >= len(buffer) or buffer[position] != '}':
            raise ValueError('INVALID_DISCLOSURE_CACHE')
    if position >= len(buffer):
        raise ValueError('INVALID_DISCLOSURE_CACHE')
    position += 1
    skip(' \r\n\t')
    if position != len(buffer) or not ended:
        raise ValueError('INVALID_DISCLOSURE_CACHE')


def write_record(db, receipt, record):
    documents = record.get('documents', {}) if isinstance(record, dict) else {}
    originals = any('content' in document for document in documents.values())
    if originals:
        metadata = {**record, 'documents': {key: {k: v for k, v in value.items() if k != 'content'}
                                         for key, value in documents.items()}}
        db.execute('INSERT INTO disclosure_records VALUES (?,?,?) ON CONFLICT(receipt) DO UPDATE SET '
                   'metadata=excluded.metadata,documents=excluded.documents',
                   (receipt, canonical(metadata), canonical(documents)))
    else:
        metadata = record
        db.execute("INSERT INTO disclosure_records VALUES (?,?,'{}') ON CONFLICT(receipt) DO UPDATE SET "
                   'metadata=excluded.metadata WHERE metadata<>excluded.metadata', (receipt, canonical(metadata)))
    return metadata


def load_records(db, legacy):
    db.execute('CREATE TABLE IF NOT EXISTS disclosure_records('
               'receipt TEXT PRIMARY KEY,metadata TEXT NOT NULL,documents TEXT NOT NULL)')
    if not db.execute("SELECT 1 FROM runtime_cache WHERE namespace='disclosure_storage_version'").fetchone():
        source = db
        row = db.execute("SELECT rowid FROM runtime_cache WHERE namespace='disclosure_records'").fetchone()
        if row is None and legacy.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_cache'").fetchone():
            source = legacy
            row = legacy.execute("SELECT rowid FROM runtime_cache WHERE namespace='disclosure_records'").fetchone()
        db.execute('BEGIN IMMEDIATE')
        try:
            if row:
                with source.blobopen('runtime_cache', 'payload', row[0], readonly=True) as blob:
                    for receipt, record in iter_records(blob):
                        write_record(db, receipt, record)
            # ponytail: retain the recovery blob to avoid journaling it at startup;
            # reclaim it offline if space matters. The marker prevents reuse.
            db.execute("INSERT INTO runtime_cache VALUES ('disclosure_storage_version','1')")
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
    return {receipt: json.loads(metadata) for receipt, metadata in db.execute('SELECT receipt,metadata FROM disclosure_records')}
