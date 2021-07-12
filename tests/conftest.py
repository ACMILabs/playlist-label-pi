import datetime

import pytest
from peewee import SqliteDatabase

from app import main
from app.main import HasTapped, Message


@pytest.fixture
def app():
    return main.app


@pytest.fixture
def database():
    """
    Setup the test database.
    """
    test_db = SqliteDatabase(':memory:')
    test_db.bind([Message, HasTapped], bind_refs=False, bind_backrefs=False)
    test_db.connect()
    test_db.create_tables([Message, HasTapped])

    HasTapped.create(has_tapped=0, tap_successful=0, tap_processing=0)

    timestamp = datetime.datetime.now().timestamp()
    Message.create(
        datetime=timestamp,
        playlist_id=1,
        media_player_id=1,
        label_id=1,
        playback_position=0,
        audio_buffer=0,
        video_buffer=0,
    )
