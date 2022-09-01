#!/bin/sh
# cron script to start app and check it is running

# put this task in crontab (crontab -e)
# * * * * * sh ~/.local/blink_camera_control/app/check_running_blink_auto_arm.sh > /dev/null 2>$1 &

PROJECT_PATH=~/.local/blink_camera_control
APP=blink_camera_auto_arm

APP_PATH="$PROJECT_PATH/app/$APP.py"
LOG_PATH="/var/log/$APP.py.log"
CURRENT_DATE=$(date +"%m/%d/%Y, %H:%M:%S")

# Wide output for ps is needed. (check: 'man ps' for your system. OPKG syntax. For ubuntu use: ps -eF)
ps www | grep -v grep | grep $APP_PATH > /dev/null 2>&1
if [ $? -eq 0 ]; then
    :
else
    # If it is not running, start it!
    echo "$CURRENT_DATE: $APP_PATH is not running. Starting it now." >> $LOG_PATH
    command="$PROJECT_PATH/venv/bin/python -u $APP_PATH"
    $command >> $LOG_PATH 2>&1 &
fi



