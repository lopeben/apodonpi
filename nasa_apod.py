#!/usr/bin/python3

import os
import sys
import ast
import json
import pytz
import requests
import datetime

from urllib.request import urlretrieve
from urllib.request import urlopen
from PIL import Image

from pprint import PrettyPrinter
pp = PrettyPrinter()

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


def main():
  date = getDateNow()
  print (date)
  pic_url = fetchAPOD(date)
  print (pic_url)
  resp = json.loads(pic_url)
  print (resp["url"])
  link = requests.get(resp["url"], stream=True).raw
  print (link)
  temporary_file = '/tmp/image.jpg'
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
    except:
        print("Unable to download video")
        os.system('youtube-dl -U') # try update the downloader 
        os.system('cat /dev/urandom > /dev/fb0')
  else:
    os.system('pkill fbi')
    os.system('/usr/bin/fbi --autozoom --noverbose --vt 1 ' + temporary_file)
    im.close()

# Using the special variable
# __name__
if __name__=="__main__":
    main()
