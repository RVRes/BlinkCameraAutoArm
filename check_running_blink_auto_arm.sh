#!/bin/sh
# Cron script to start app and check it is running.

# Start script with syslog support.
# All messages will be in the log diagnostic router tab.
# Put this task in crontab (crontab -e).
# Add S05crond file to /opt/etc/init.d to put cron upon autostart.
# * * * * * sh /opt/scripts/blink_auto_arm/check_running_blink_auto_arm.sh > /dev/null 2>$1 &

PROJECT_PATH=/opt/scripts/blink_auto_arm
APP=blink_camera_auto_arm

APP_PATH="$PROJECT_PATH/$APP.py"

echo "test-logger" | logger -t $APP

# Wide output for ps is needed.
# (check: 'man ps' for your system. OPKG syntax. For ubuntu use: ps -eF)
ps www | grep -v grep | grep $APP_PATH > /dev/null 2>&1
if [ $? -eq 0 ]; then
    :
else
    # If it is not running, start it!
    echo "$APP_PATH is not running. Starting it now." | logger -t $APP
    command="$PROJECT_PATH/venv/bin/python -u $APP_PATH"
    $command | logger -t $APP &
fi
