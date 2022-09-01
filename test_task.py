from time import sleep, strftime, localtime
from random import randint


def time_now():
    """Return formatted string with local date and time"""
    return strftime("%m/%d/%Y, %H:%M:%S", localtime())


seed = randint(10000, 99999)
print(f'{time_now()}: Test task is started! Seed: {seed}')
while True:
    sleep(15)
    print(f'{time_now()}: Working! Seed: {seed}')
