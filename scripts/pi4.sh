#!/bin/bash

# Remove the X server lock file so ours starts cleanly
rm /tmp/.X0-lock &>/dev/null || true

# Set the display to use
export DISPLAY=:0

# Set the DBUS address for sending around system messages
export DBUS_SYSTEM_BUS_ADDRESS=unix:path=/host/run/dbus/system_bus_socket

# start desktop manager
echo "STARTING X"
sleep 2
startx &
sleep 20

# Hide the cursor
unclutter -display :0 -idle 0.1 &

# Start Flask
python3 -u -m app.main &

sleep 10

# Rotate the display if needed
ROTATE_SCREEN="${ROTATE_SCREEN:-false}"
if [ "$ROTATE_SCREEN" == left ]
then
xrandr -o left
# Rotate Raspberry Pi screen touch interface
xinput set-prop "FT5406 memory based driver" --type=float "Coordinate Transformation Matrix" 0 -1 1 1 0 0 0 0 1
# Rotate 12.3" screen touch interface
xinput set-prop "ILITEK ILITEK-TP" --type=float "Coordinate Transformation Matrix" 0 -1 1 1 0 0 0 0 1
fi
if [ "$ROTATE_SCREEN" == right ]
then
xrandr -o right
# Rotate Raspberry Pi screen touch interface
xinput set-prop "FT5406 memory based driver" --type=float "Coordinate Transformation Matrix" 0 1 0 -1 0 1 0 0 1
# Rotate 12.3" screen touch interface
xinput set-prop "ILITEK ILITEK-TP" --type=float "Coordinate Transformation Matrix" 0 1 0 -1 0 1 0 0 1
fi

# Launch chromium browser in fullscreen on that page
SCREEN_SCALE="${SCREEN_SCALE:-1.0}"
chromium-browser --app=http://localhost:8081 --start-fullscreen --no-sandbox --user-data-dir --kiosk --disable-dev-shm-usage --disable-backing-store-limit --enable-logging=stderr --v=1 --force-device-scale-factor=$SCREEN_SCALE

# For debugging
echo "Chromium browser exited unexpectedly."
free -h
echo "End of pi.sh ..."
