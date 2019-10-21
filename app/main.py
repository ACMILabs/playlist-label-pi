import json
import os
import socket
import sqlite3
from threading import Thread

import requests
from flask import Flask, jsonify, render_template, request
from kombu import Connection, Exchange, Queue
from peewee import CharField, FloatField, IntegerField, Model, SqliteDatabase
from playhouse.shortcuts import model_to_dict
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration

from errors import HTTPError


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
SENTRY_ID = os.getenv('SENTRY_ID')

BALENA_APP_ID = os.getenv('BALENA_APP_ID')
BALENA_SERVICE_NAME = os.getenv('BALENA_SERVICE_NAME')
BALENA_SUPERVISOR_ADDRESS = os.getenv('BALENA_SUPERVISOR_ADDRESS')
BALENA_SUPERVISOR_API_KEY = os.getenv('BALENA_SUPERVISOR_API_KEY')

# Setup Sentry
sentry_sdk.init(
    dsn=SENTRY_ID,
    integrations=[FlaskIntegration()]
)
amqp_url = f'amqp://{RABBITMQ_MEDIA_PLAYER_USER}:{RABBITMQ_MEDIA_PLAYER_PASS}@{RABBITMQ_MQTT_HOST}:{AMQP_PORT}//'
queue_name = f'mqtt-subscription-playback_{XOS_MEDIA_PLAYER_ID}'
routing_key = f'mediaplayer.{XOS_MEDIA_PLAYER_ID}'

media_player_exchange = Exchange('amq.topic', 'direct', durable=True)
playback_queue = Queue(queue_name, exchange=media_player_exchange, routing_key=routing_key)

app = Flask(__name__)
cached_playlist_json = f'playlist_{XOS_PLAYLIST_ID}.json'
# instantiate the peewee database
db = SqliteDatabase('message.db')


class Message(Model):
    datetime = CharField(primary_key=True)
    label_id = IntegerField()
    playlist_id = IntegerField()
    media_player_id = IntegerField()
    playback_position = FloatField()
    audio_buffer = FloatField(null=True)
    video_buffer = FloatField(null=True)
    class Meta:
        database = db


def download_playlist_label():
    # Download Playlist JSON from XOS
    try:
        playlist_label_json = requests.get(f'{XOS_API_ENDPOINT}playlists/{XOS_PLAYLIST_ID}/').json()

        # Write it to the file system
        with open(cached_playlist_json, 'w') as outfile:
            json.dump(playlist_label_json, outfile)

    except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError) as e:
        print(f'Error downloading playlist JSON from XOS: {e}')
        sentry_sdk.capture_exception(e)


def process_media(body, message):
    Message.create(
        datetime = body['datetime'],
        playlist_id = body.get('playlist_id', 0),
        media_player_id = body.get('media_player_id', 0),
        label_id = body.get('label_id', 0),
        playback_position = body.get('playback_position', 0),
        audio_buffer = body.get('audio_buffer', 0),
        video_buffer = body.get('video_buffer', 0),
    )
    # clear out other messages beyond the last 5
    delete_records = Message.delete().where(
        Message.datetime.not_in(Message.select(Message.datetime).order_by(Message.datetime.desc()).limit(5))
    )
    delete_records.execute()

    try:
        message.ack()

    except TimeoutError as e:
        template = 'An exception of type {0} occurred. Arguments:\n{1!r}'
        message = template.format(type(e).__name__, e.args)
        print(message)
        sentry_sdk.capture_exception(e)

        # TODO: Do we need to restart the container?
        # restart_app_container()


def restart_app_container(self):
    try:
        balena_api_url = f'{BALENA_SUPERVISOR_ADDRESS}/v2/applications/{BALENA_APP_ID}/restart-service?apikey={BALENA_SUPERVISOR_API_KEY}'
        json = {
            "serviceName": BALENA_SERVICE_NAME
        }
        response = requests.post(balena_api_url, json=json)
        response.raise_for_status()
    except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError) as e:
        message = f'Failed to restart the Media Player container with error: {e}'
        print(message)
        sentry_sdk.capture_exception(e)


def get_events():
    # connections
    with Connection(amqp_url) as conn:
        # consume
        with conn.Consumer(playback_queue, callbacks=[process_media]) as consumer:
            # Process messages and handle events on all channels
            while True:
                try:
                    conn.drain_events(timeout=2)
                except (socket.timeout, TimeoutError) as exception:
                    # TODO: make robust
                    sentry_sdk.capture_exception(exception)


@app.errorhandler(HTTPError)
def handle_http_error(error):
    """
    Format error for response.
    """
    response = jsonify(error.to_dict())
    response.status_code = error.status_code
    sentry_sdk.capture_exception(error)
    return response


@app.route('/')
def playlist_label():
    # Read in the cached JSON
    with open(cached_playlist_json, encoding='utf-8') as json_file:
        json_data = json.load(json_file)

    return render_template(
        'playlist.html',
        playlist_json=json_data,
        mqtt={
            'host': RABBITMQ_MQTT_HOST,
            'port': RABBITMQ_MQTT_PORT,
            'username': RABBITMQ_MEDIA_PLAYER_USER,
            'password': RABBITMQ_MEDIA_PLAYER_PASS
        },
        xos={
            'playlist_endpoint': f'{XOS_API_ENDPOINT}playlists/',
            'media_player_id': XOS_MEDIA_PLAYER_ID
        }
    )


@app.route('/api/playlist/')
def playlist_json():
    # Read in the cached JSON
    with open(cached_playlist_json, encoding='utf-8') as json_file:
        json_data = json.load(json_file)
    
    return jsonify(json_data)


@app.route('/api/taps/', methods=['POST'])
def collect_item():
    """
    Collect a tap and forward it on to XOS with the label ID.
    """
    xos_tap = dict(request.get_json())
    record = model_to_dict(Message.select().order_by(Message.datetime.desc()).get())
    xos_tap['label'] = record.pop('label_id', None)
    xos_tap.setdefault('data', {})['playlist_info'] = record
    headers = {'Authorization': 'Token ' + AUTH_TOKEN}
    response = requests.post(XOS_TAPS_ENDPOINT, json=xos_tap, headers=headers)
    if response.status_code != requests.codes['created']:
        raise HTTPError('Could not save tap to XOS.')
    return jsonify(xos_tap), response.status_code

if __name__ == '__main__':
    db.create_tables([Message])
    download_playlist_label()
    Thread(target=get_events).start()
    app.run(host='0.0.0.0', port=PLAYLIST_LABEL_PORT)
