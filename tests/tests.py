import datetime
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from app import cache, main
from app.cache import create_cache
from app.main import HasTapped, Message, PlaylistLabel


class MockResponse:
    def __init__(self, json_data, status_code):
        self.content = json.loads(json_data)
        self.status_code = status_code

    def json(self):
        return self.content

    def raise_for_status(self):
        return None


def mocked_requests_get(*args, **kwargs):
    if args[0] == 'https://xos.acmi.net.au/api/playlists/1/':
        with open('tests/data/playlist.json', 'r') as the_file:
            return MockResponse(the_file.read(), 200)
    if args[0] == 'https://xos.acmi.net.au/api/playlists/2/':
        with open('tests/data/playlist_no_label.json', 'r') as the_file:
            return MockResponse(the_file.read(), 200)

    raise Exception("No mocked sample data for request: "+args[0])


def mocked_requests_post(*args, **kwargs):
    request_url = args[0]
    if '/api/taps/' in request_url:
        with open('tests/data/xos_tap.json', 'r') as the_file:
            return MockResponse(the_file.read(), 201)
    if '/api/bad-uri/' in request_url:
        return MockResponse('{}', 404)

    raise Exception("No mocked sample data for request: "+args[0])


@patch('requests.get', MagicMock(side_effect=mocked_requests_get))
def test_create_cache(capsys):
    """
    Test the create_cache method downloads an XOS Playlist and saves it to the cache directory.
    """
    # capsys.disabled forwards stdout and stderr
    with capsys.disabled():
        create_cache()
        with open(f'{main.CACHE_DIR}playlist_1.json', 'r') as playlist_cache:
            playlist = json.loads(playlist_cache.read())['playlist_labels']
        assert len(playlist) == 3
        assert playlist[0]['label']['title'] == '<p>Test pattern</p>'


@pytest.mark.usefixtures('database')
def test_process_media():
    """
    Test the process_media function creates a valid Message.
    """

    with open('tests/data/message.json', 'r') as the_file:
        message_broker_json = json.loads(the_file.read())

    message_broker_json['datetime'] = datetime.datetime.now()
    playlistlabel = PlaylistLabel()
    mock = MagicMock()
    playlistlabel.process_media(message_broker_json, mock)
    saved_message = Message.get(Message.datetime == message_broker_json['datetime'])

    mock.ack.assert_called_once()
    assert message_broker_json['label_id'] == saved_message.label_id


def test_route_playlist_label(client):
    """
    Test that the root route renders the expected data.
    """

    response = client.get('/')

    assert b'"xos_media_player_id": "%s"' % main.XOS_MEDIA_PLAYER_ID.encode() in response.data
    assert b'"mqtt_host": "track.acmi.net.au"' in response.data
    assert response.status_code == 200


@patch('requests.get', MagicMock(side_effect=mocked_requests_get))
def test_route_playlist_label_with_no_label(client):
    """
    Test that the playlist route returns the expected data
    when a playlist item doesn't have a label.
    """

    cache.XOS_PLAYLIST_ID = 2
    create_cache()
    response = client.get('/')
    response_data = response.data.decode('utf-8')

    assert 'resource' not in response_data
    assert response.status_code == 200


@patch('requests.get', MagicMock(side_effect=mocked_requests_get))
def test_route_playlist_json(client):
    """
    Test that the playlist route returns the expected data.
    """

    cache.XOS_PLAYLIST_ID = 1
    create_cache()
    response = client.get('/api/playlist/')

    assert b'Test pattern' in response.data
    assert response.status_code == 200


@pytest.mark.usefixtures('database')
@patch('requests.post', MagicMock(side_effect=mocked_requests_post))
def test_route_collect_item(client):
    """
    Test that the collect a tap route forwards the expected data to XOS.
    """

    with open('tests/data/lens_tap.json', 'r') as the_file:
        lens_tap_data = the_file.read()

    response = client.post(
        '/api/taps/',
        data=lens_tap_data,
        headers={'Content-Type': 'application/json'}
    )

    assert response.json["nfc_tag"]["short_code"] == "nbadbb"
    assert response.status_code == 201

    has_tapped = HasTapped.get_or_none(tap_processing=1)
    assert has_tapped.has_tapped == 1
    assert has_tapped.tap_successful == 1


@patch('sentry_sdk.capture_exception', side_effect=MagicMock())
def test_send_error_sends_on_repetition_and_repeat_every(capture_exception):
    """
    Test that the send_error function only sends the error
    on repetition 5 and every 20 times.
    """
    playlist_label = PlaylistLabel()

    # call send_error 4 times and assert it doesn't send the error
    for _ in range(4):
        playlist_label.send_error('rmq_conn', None, on_rep=5, every=20, units='instances')
    assert capture_exception.call_count == 0

    # make sure the error is sent on the 5th time
    playlist_label.send_error('rmq_conn', None, on_rep=5, every=20, units='instances')
    assert capture_exception.call_count == 1

    # make sure the error is not sent before another 20 times
    for _ in range(19):
        playlist_label.send_error('rmq_conn', None, on_rep=5, every=20, units='instances')
    assert capture_exception.call_count == 1

    # make sure the error is sent on the next 20th time
    playlist_label.send_error('rmq_conn', None, on_rep=5, every=20, units='instances')
    assert capture_exception.call_count == 2


@patch('sentry_sdk.capture_exception', side_effect=MagicMock())
def test_send_error_sends_on_repetition_and_repeat_every_1_second(capture_exception):
    """
    Test that the send_error function only sends the error
    on repetition 5 and every second.
    """
    playlist_label = PlaylistLabel()

    # call send_error 4 times and assert it doesn't send the error
    for _ in range(4):
        playlist_label.send_error('rmq_conn', None, on_rep=5, every=1, units='seconds')
    assert capture_exception.call_count == 0

    # make sure the error is sent on the 5th time
    playlist_label.send_error('rmq_conn', None, on_rep=5, every=1, units='seconds')
    assert capture_exception.call_count == 1

    # make sure the error is not sent before 1 second has passed
    for _ in range(10):
        playlist_label.send_error('rmq_conn', None, on_rep=5, every=1, units='seconds')
    assert capture_exception.call_count == 1

    time.sleep(1.5)

    # make sure the error is sent after 1 second
    playlist_label.send_error('rmq_conn', None, on_rep=5, every=1, units='seconds')
    assert capture_exception.call_count == 2


@pytest.mark.usefixtures('database')
@patch('app.main.XOS_TAPS_ENDPOINT', 'https://xos.acmi.net.au/api/bad-uri/')
@patch('requests.post', MagicMock(side_effect=mocked_requests_post))
def test_tap_received_xos_error(client):
    """
    Test that a tap fails correctly for an XOS error
    """
    with open('tests/data/lens_tap.json', 'r') as the_file:
        lens_tap_data = the_file.read()

    response = client.post(
        '/api/taps/',
        data=lens_tap_data,
        headers={'Content-Type': 'application/json'}
    )

    assert response.status_code == 400

    has_tapped = HasTapped.get_or_none(tap_processing=1)
    assert has_tapped.has_tapped == 1
    assert has_tapped.tap_successful == 0


@pytest.mark.usefixtures('database')
@patch('requests.post', MagicMock(side_effect=mocked_requests_post))
def test_tap_received_while_processing_still_creates(client):
    """
    Test that if an old tap is still being processed by the UI, new taps are still created
    """
    has_tapped = HasTapped.get_or_none(tap_processing=0)
    has_tapped.tap_processing = 1
    has_tapped.save()

    with open('tests/data/lens_tap.json', 'r') as the_file:
        lens_tap_data = the_file.read()

    response = client.post(
        '/api/taps/',
        data=lens_tap_data,
        headers={'Content-Type': 'application/json'}
    )

    assert response.status_code == 201
