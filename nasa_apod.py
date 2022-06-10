#!/usr/bin/python3

import os
import sys
import ast
import json
import pytz
import evdev
import requests
import datetime
import functools

from PIL import Image
from pprint import PrettyPrinter
from apscheduler.schedulers.background import BackgroundScheduler

pp = PrettyPrinter()
scheduler = BackgroundScheduler(timezone="Asia/Manila")

isVideo = False

def fetchAPOD( date ):
  with open('./site.txt') as f:
    site_data = f.read()
    site_data = ast.literal_eval(site_data)

  URL_APOD = site_data['url']
  params = {
    'api_key' : site_data['key'],
    'date' : date,
    'hd' : 'True'
  }

  response = requests.get(URL_APOD,params=params).json()
  #pp.pprint(response)
  return json.dumps(response)


def getDateNow():
  dt = datetime.datetime.now(pytz.timezone('US/Eastern'))
  datestr = str(dt.year) + '-' + str(dt.month) + '-'  + str(dt.day)
  #datestr = str(2022) + '-' + str(2) + '-'  + str(10) # sample
  return datestr


@scheduler.scheduled_job('cron', day_of_week='mon-sun', hour=6)
def fetchArtifact():
  global isVideo
  temporary_file = '/tmp/image.jpg'

  date = getDateNow()
  print (date)
  
  pic_url = fetchAPOD(date)
  print (pic_url)
  
  resp = json.loads(pic_url)
  print (resp["url"])
  
  link = requests.get(resp["url"], stream=True).raw
  print (link)
  
  try:
    im = Image.open(link)
    #print("Image open")
    im.save(temporary_file)
    #print("Image saved")
  except:
    try:
        os.system('rm /tmp/videofile*')
        os.system('youtube-dl '+ resp["url"] + ' -o /tmp/videofile')
        #Refer: https://superuser.com/questions/547296/resizing-videos-with-ffmpeg-avconv-to-fit-into-static-sized-player/1136305#1136305
        os.system('ffmpeg -i /tmp/videofile* -vf scale=480:320:force_original_aspect_ratio=decrease:eval=frame,pad=480:320:-1:-1:color=black /tmp/videofile_320p.mp4')
        os.system('mplayer -x 480 -y 320 -nosound -vo fbdev:/dev/fb1 /tmp/videofile_320p.mp4')
        isVideo = True
    except:
        print("Unable to download video")
        os.system('youtube-dl -U') # try update the downloader 
        os.system('cat /dev/urandom > /dev/fb0')
  else:
    os.system('pkill fbi')
    os.system('/usr/bin/fbi --autozoom --noverbose --vt 1 ' + temporary_file)
    isVideo = False
    im.close()


def displayCallback():
  if isVideo:
    os.system('mplayer -x 480 -y 320 -nosound -vo fbdev:/dev/fb1 /tmp/videofile_320p.mp4')


def screenPressed(callback):
  callback()



def main():
  size = 2
  sample_buffer = ['' for x in range(size)]

  device = evdev.InputDevice('/dev/input/event0')

  pattern_buffer = ['up', 'down']

  for event in device.read_loop():
    if event.type == evdev.ecodes.EV_KEY:
      event_string = str(evdev.categorize(event))
      event_list = event_string.split(",")
      # print(event_list[2])

      sample_buffer.insert(0, event_list[2].strip())
      sample_buffer.pop(size)
      # print(sample_buffer)

      # Compare the two arrays to check touch 
      param_a = lambda x, y: x and y
      param_b = map(lambda a, b: a == b, sample_buffer, pattern_buffer)
      if functools.reduce(param_a, param_b, True):
        screenPressed(displayCallback)



if __name__ == '__main__':
    try:
        ret = main()
    except (KeyboardInterrupt, EOFError):
        ret = 0
    sys.exit(ret)