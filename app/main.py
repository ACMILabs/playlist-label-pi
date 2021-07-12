import datetime
import json
import os
import socket
import time
from threading import Thread

import kombu
import requests
import sentry_sdk
from flask import Flask, Response, jsonify, render_template, request
from kombu import Connection, Exchange, Queue
from peewee import (CharField, FloatField, IntegerField, Model,
                    OperationalError, SqliteDatabase)
from playhouse.shortcuts import model_to_dict
from sentry_sdk.integrations.flask import FlaskIntegration

from app.errors import HTTPError

XOS_API_ENDPOINT = os.getenv('XOS_API_ENDPOINT')
XOS_TAPS_ENDPOINT = os.getenv('XOS_TAPS_ENDPOINT', f'{XOS_API_ENDPOINT}taps/')
AUTH_TOKEN = os.getenv('AUTH_TOKEN')
XOS_PLAYLIST_ID = os.getenv('XOS_PLAYLIST_ID', '1')
XOS_MEDIA_PLAYER_ID = os.getenv('XOS_MEDIA_PLAYER_ID', '1')
PLAYLIST_LABEL_PORT = 8081
RABBITMQ_MQTT_HOST = os.getenv('RABBITMQ_MQTT_HOST')
RABBITMQ_MQTT_PORT = os.getenv('RABBITMQ_MQTT_PORT')
RABBITMQ_MEDIA_PLAYER_USER = os.getenv('RABBITMQ_MEDIA_PLAYER_USER')
RABBITMQ_MEDIA_PLAYER_PASS = os.getenv('RABBITMQ_MEDIA_PLAYER_PASS')
AMQP_PORT = os.getenv('AMQP_PORT')
RABBITMQ_RETRY_SECONDS = int(os.getenv('RABBITMQ_RETRY_SECONDS', '2'))
SENTRY_ID = os.getenv('SENTRY_ID')

BALENA_APP_ID = os.getenv('BALENA_APP_ID')
BALENA_SERVICE_NAME = os.getenv('BALENA_SERVICE_NAME')
BALENA_SUPERVISOR_ADDRESS = os.getenv('BALENA_SUPERVISOR_ADDRESS')
BALENA_SUPERVISOR_API_KEY = os.getenv('BALENA_SUPERVISOR_API_KEY')
DEBUG = os.getenv('DEBUG', 'false').lower() == "true"
CACHE_DIR = os.getenv('CACHE_DIR', '/data/')

LABEL_TEMPLATE = os.getenv('LABEL_TEMPLATE', 'playlist.html')
COLLECT_POSITION = os.getenv('COLLECT_POSITION', None)

# Setup Sentry
sentry_sdk.init(
    dsn=SENTRY_ID,
    integrations=[FlaskIntegration()]
)
AMQP_URL = f'amqp://{RABBITMQ_MEDIA_PLAYER_USER}:{RABBITMQ_MEDIA_PLAYER_PASS}'\
           f'@{RABBITMQ_MQTT_HOST}:{AMQP_PORT}//'
QUEUE_NAME = f'mqtt-subscription-playback_{XOS_MEDIA_PLAYER_ID}'
ROUTING_KEY = f'mediaplayer.{XOS_MEDIA_PLAYER_ID}'

MEDIA_PLAYER_EXCHANGE = Exchange('amq.topic', 'direct', durable=True)
PLAYBACK_QUEUE = Queue(QUEUE_NAME, exchange=MEDIA_PLAYER_EXCHANGE, routing_key=ROUTING_KEY)

app = Flask(__name__)  # pylint: disable=C0103
CACHED_PLAYLIST_JSON = f'playlist_{XOS_PLAYLIST_ID}.json'
# instantiate the peewee database
db = SqliteDatabase('message.db')  # pylint: disable=C0103


class Message(Model):  # pylint: disable=R0903
    datetime = CharField(primary_key=True)
    label_id = IntegerField()
    playlist_id = IntegerField()
    media_player_id = IntegerField()
    playback_position = FloatField()
    audio_buffer = FloatField(null=True)
    video_buffer = FloatField(null=True)

    class Meta:  # pylint: disable=R0903
        database = db


class PlaylistLabel():
    """
    A playlist label that communicates with XOS to download labels,
    and sends lens taps back to XOS with the label tapped.
    """

    def __init__(self):
        self.playlist = None
        self.errors_history = {}

    @staticmethod
    def process_media(body, message):
        """
        Store the message received from RabbitMQ.
        """
        try:
            message.ack()

            Message.create(
                datetime=body['datetime'],
                playlist_id=body.get('playlist_id', 0),
                media_player_id=body.get('media_player_id', 0),
                label_id=body.get('label_id', 0),
                playback_position=body.get('playback_position', 0),
                audio_buffer=body.get('audio_buffer', 0),
                video_buffer=body.get('video_buffer', 0),
            )
            # clear out other messages beyond the last 5
            delete_records = Message.delete().where(
                Message.datetime.not_in(
                    Message.select(Message.datetime).order_by(Message.datetime.desc()).limit(5)
                )
            )
            delete_records.execute()

        except TimeoutError as exception:
            template = 'An exception of type {0} occurred. Arguments:\n{1!r}'
            message = template.format(type(exception).__name__, exception.args)
            print(message)
            sentry_sdk.capture_exception(exception)

    def consume(self, conn):
        """
        Try to consume from RabbitMQ queue and store the received message.
        """
        connection_errors = conn.connection_errors + (kombu.exceptions.OperationalError,)
        try:
            conn.ensure_connection(max_retries=3)
            with conn.Consumer(PLAYBACK_QUEUE, callbacks=[self.process_media]):
                # Process messages and handle events on all channels
                while True:
                    try:
                        conn.drain_events(timeout=2)
                        resolved_timeout = self.clear_error_history('media_player_timeout')
                        if resolved_timeout:
                            print(f'Automatically resolved: {resolved_timeout}. '
                                  'Now receiving messages.')
                        resolved_conn = self.clear_error_history('rabbitmq_conn_error')
                        if resolved_conn:
                            print(f'Automatically resolved: {resolved_conn}. '
                                  'Connection reestablished.')
                    except socket.timeout as exception:
                        print(f'Stopped receiving messages from media player {XOS_MEDIA_PLAYER_ID}')
                        self.send_error('media_player_timeout', exception, every=3600)
                        conn.heartbeat_check()
        except connection_errors as conn_error:
            # error with the connection, wait and try to connect again
            print(f'Error connecting to RabbitMQ server: {conn_error}')
            self.send_error('rabbitmq_conn_error', conn_error, on_rep=3, every=3600)
            print(f'Retrying in {RABBITMQ_RETRY_SECONDS} seconds')
            time.sleep(RABBITMQ_RETRY_SECONDS)

    def get_events(self):
        """
        Create a connection to RabbitMQ server and try to consume.
        """
        while True:
            with Connection(AMQP_URL, heartbeat=5, connect_timeout=5) as conn:
                self.consume(conn)

    def send_error(self, error_name, error, on_rep=5, every=100, units='seconds'):
        # pylint: disable=too-many-arguments
        """
        Attempt to send an error to sentry.
        Send to Sentry for the first time when calling send_error for the `on_rep`th time.
        Subsequently, send to Sentry on every `every` `units` (e.g every 100 seconds)
        if the error has not been fixed.

        This function helps to not report sporadic connection errors that are automatically
        resolved.
        Also, if an error is persistent, this function helps to not flood Sentry.

        :param error_name: The name of the error used to keep a history
        :type error_name: str
        :param error: The error that is being sent to Sentry
        :type error: :class:`Exception`
        :param on_rep: The repetition when the error is actually sent for the first time to Sentry
        :type on_rep: int
        :param every: The number of instances or seconds a error is re-sent to Sentry
        :type every: int
        :param units: Either 'instances' or 'seconds' to be used alonside the `every` arg
        :type units: str
        """
        try:
            error_history = self.errors_history[error_name]
        except KeyError:
            error_history = {
                'error': error,
                'consecutive_instances': 0,
                'last_sent_time': None
            }
            self.errors_history[error_name] = error_history

        error_history['consecutive_instances'] += 1

        # send for the first time on the `on_rep`th time
        if error_history['consecutive_instances'] == on_rep:
            sentry_sdk.capture_exception(error)
            error_history['last_sent_time'] = datetime.datetime.now()
            return
        if error_history['consecutive_instances'] < on_rep:
            return

        # subsequent sendings go every `every` `units`
        if units == 'seconds':
            time_since_last = datetime.datetime.now() - error_history['last_sent_time']
            if time_since_last.seconds >= every:
                sentry_sdk.capture_exception(error)
                error_history['last_sent_time'] = datetime.datetime.now()
        elif units == 'instances':
            if (error_history['consecutive_instances'] - on_rep) % every == 0:
                sentry_sdk.capture_exception(error)
                error_history['last_sent_time'] = datetime.datetime.now()
        else:
            print('Invalid units')

    def clear_error_history(self, error_name):
        """
        Remove the history of an error.
        This method should be called whenever a problem (e.g. network disruption)
        has been resolved.

        :param error_name: The name given to the error in `send_error`.
        :type error_name: str
        :return: The error whose history was deleted.
        :rtype: :class:`Exception`
        """
        try:
            return self.errors_history.pop(error_name)['error']
        except KeyError:
            return None


@app.errorhandler(HTTPError)
def handle_http_error(error):
    """
    Format error for response.
    """
    response = jsonify(error.to_dict())
    response.status_code = error.status_code
    sentry_sdk.capture_exception(error)
    return response


class HasTapped(Model):  # pylint: disable=R0903
    has_tapped = IntegerField()
    tap_successful = IntegerField()
    tap_processing = IntegerField()

    class Meta:  # pylint: disable=R0903
        database = db


@app.route('/')
def playlist_label():
    # Read in the cached JSON
    json_data = {}
    try:
        with open(f'{CACHE_DIR}{CACHED_PLAYLIST_JSON}', encoding='utf-8') as json_file:
            json_data = json.load(json_file)

        # Remove playlist items that don't have a label
        for item in list(json_data['playlist_labels']):
            if item['label'] is None:
                json_data['playlist_labels'].remove(item)

        # Calculate classnames
        collect_classname = f'collect {COLLECT_POSITION}' if COLLECT_POSITION else 'collect'

        return render_template(
            LABEL_TEMPLATE,
            playlist_json=json_data,
            playlist_json_rendered=json.dumps(json_data),
            mqtt={
                'host': RABBITMQ_MQTT_HOST,
                'port': RABBITMQ_MQTT_PORT,
                'username': RABBITMQ_MEDIA_PLAYER_USER,
                'password': RABBITMQ_MEDIA_PLAYER_PASS
            },
            xos={
                'playlist_endpoint': f'{XOS_API_ENDPOINT}playlists/',
                'media_player_id': XOS_MEDIA_PLAYER_ID
            },
            is_preview='false',
            collect_classname=collect_classname
        )
    except FileNotFoundError:
        print(f'Couldn\'t open cached playlist JSON: {CACHE_DIR}{CACHED_PLAYLIST_JSON}')
        return render_template('no_playlist.html')


@app.route('/api/playlist/')
def playlist_json():
    # Read in the cached JSON
    json_data = {}
    try:
        with open(f'{CACHE_DIR}{CACHED_PLAYLIST_JSON}', encoding='utf-8') as json_file:
            json_data = json.load(json_file)
    except FileNotFoundError:
        pass

    return jsonify(json_data)


@app.route('/api/taps/', methods=['POST'])
def collect_item():
    """
    Collect a tap and forward it on to XOS with the label ID.
    """
    tap_to_process = HasTapped.get_or_none(tap_processing=0)
    if tap_to_process:
        tap_to_process.tap_processing = 1
        tap_to_process.save()

    xos_tap = dict(request.get_json())
    record = model_to_dict(Message.select().order_by(Message.datetime.desc()).get())
    xos_tap['label'] = record.pop('label_id', None)
    xos_tap.setdefault('data', {})['playlist_info'] = record
    headers = {'Authorization': 'Token ' + AUTH_TOKEN}
    response = requests.post(XOS_TAPS_ENDPOINT, json=xos_tap, headers=headers)

    if response.status_code != requests.codes['created']:
        if tap_to_process:
            tap_to_process.tap_successful = 0
            tap_to_process.has_tapped = 1
            tap_to_process.save()
        raise HTTPError('Could not save tap to XOS.')

    if tap_to_process:
        tap_to_process.tap_successful = 1
        tap_to_process.has_tapped = 1
        tap_to_process.save()

    return response.json(), response.status_code


def event_stream():
    while True:
        time.sleep(0.1)
        try:
            has_tapped = HasTapped.get_or_none(tap_processing=1, has_tapped=1)
            if has_tapped:
                tap_event_message = f'data: {{ "tap_successful": {has_tapped.tap_successful} }}\n\n'
                has_tapped.has_tapped = 0
                has_tapped.tap_processing = 0
                has_tapped.tap_successful = 0
                has_tapped.save()
                yield tap_event_message
        except OperationalError as exception:
            template = 'An exception of type {0} {1!r} occurred in event_stream '\
                       'trying to update HasTapped.'
            message = template.format(type(exception).__name__, exception.args)
            if DEBUG:
                print(message)


@app.route('/api/tap-source/')
def tap_source():
    return Response(event_stream(), mimetype="text/event-stream")


if __name__ == '__main__':
    db.create_tables([Message, HasTapped])
    HasTapped.create(has_tapped=0, tap_successful=0, tap_processing=0)
    playlistlabel = PlaylistLabel()  # pylint: disable=C0103
    Thread(target=playlistlabel.get_events).start()
    app.run(host='0.0.0.0', port=PLAYLIST_LABEL_PORT)
