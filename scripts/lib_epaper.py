#!/usr/bin/env python

#######################################################################
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#######################################################################

# Driver for the Waveshare 2.13inch e-Paper HAT V4 (SSD1680, 250x122, black/white).
# Command sequences follow Waveshare's epd2in13_V4.py reference driver.
#
# The class mimics the subset of the luma device API used by display.py
# (width, height, mode, persist, capabilities(), contrast(), display()).
#
# E-paper is slow and must not be driven with high voltage permanently, so:
# - frames are written by a worker thread (display() never blocks), only the latest frame is drawn
# - unchanged frames are skipped
# - updates are partial refreshes, with a full refresh every FULL_REFRESH_EVERY updates
#   or FULL_REFRESH_SEC seconds to remove ghosting
# - the panel goes into deep sleep after SLEEP_AFTER_SEC seconds without updates

import atexit
import sys
import threading
import time

import RPi.GPIO as GPIO
import spidev

from PIL import Image, ImageChops

class epd2in13_v4(object):

	PANEL_WIDTH			= 122
	PANEL_HEIGHT		= 250

	PIN_RST				= 17
	PIN_DC				= 25
	PIN_BUSY			= 24
	PIN_PWR				= 18

	FULL_REFRESH_EVERY	= 30
	FULL_REFRESH_SEC	= 600
	SLEEP_AFTER_SEC		= 30
	BUSY_TIMEOUT_SEC	= 10

	# display.py: minimum seconds between statusbar-only redraws
	statusbar_refresh_sec	= 60

	def __init__(self, spi_port=0, spi_device=0, inverse=False):
		self.persist	= False
		self.mode		= '1'
		self.rotate		= 0
		self.width		= self.PANEL_HEIGHT	# landscape
		self.height		= self.PANEL_WIDTH

		# False: black text on white background
		self.__inverse	= inverse

		GPIO.setmode(GPIO.BCM)
		GPIO.setwarnings(False)
		GPIO.setup(self.PIN_RST, GPIO.OUT)
		GPIO.setup(self.PIN_DC, GPIO.OUT)
		GPIO.setup(self.PIN_PWR, GPIO.OUT)
		GPIO.setup(self.PIN_BUSY, GPIO.IN)
		GPIO.output(self.PIN_PWR, 1)

		self.__spi	= spidev.SpiDev()
		self.__spi.open(spi_port, spi_device)
		self.__spi.max_speed_hz	= 4000000
		self.__spi.mode			= 0

		self.__lock				= threading.Condition()
		self.__pending			= None
		self.__running			= True

		self.__shown			= None	# panel buffer image currently on screen
		self.__awake			= False
		self.__partial_count	= 0
		self.__last_full		= 0
		self.__last_update		= 0

		self.__worker	= threading.Thread(target=self.__run, daemon=True)
		self.__worker.start()

		atexit.register(self.cleanup)

	# luma compatible interface

	def capabilities(self, width=None, height=None, rotate=0, mode='1'):
		# the panel has a fixed resolution, configured width/height are ignored
		self.rotate	= rotate % 4
		if self.rotate in (0, 2):
			self.width, self.height	= self.PANEL_HEIGHT, self.PANEL_WIDTH
		else:
			self.width, self.height	= self.PANEL_WIDTH, self.PANEL_HEIGHT
		self.mode	= mode

	def contrast(self, *args, **kwargs):
		return()

	def display(self, image):
		with self.__lock:
			self.__pending	= image.copy()
			self.__lock.notify()

	def cleanup(self):
		with self.__lock:
			if not self.__running:
				return()
			self.__running	= False
			self.__lock.notify()

		self.__worker.join(timeout=self.BUSY_TIMEOUT_SEC + 5)

		try:
			if self.__awake:
				self.__sleep()
			self.__spi.close()
		except Exception as e:
			print(f'e-paper cleanup failed: {e}', file=sys.stderr)

	# worker

	def __run(self):
		while True:
			with self.__lock:
				if self.__pending is None and self.__running:
					timeout	= self.SLEEP_AFTER_SEC if self.__awake else None
					self.__lock.wait(timeout=timeout)

				if not self.__running:
					return()

				image			= self.__pending
				self.__pending	= None

			try:
				if image is None:
					if self.__awake and time.time() - self.__last_update >= self.SLEEP_AFTER_SEC:
						self.__sleep()
				else:
					self.__update(self.__to_panel(image))
			except Exception as e:
				print(f'e-paper update failed: {e}', file=sys.stderr)
				self.__awake	= False

	def __to_panel(self, image):
		image	= image.convert('L')

		if not self.__inverse:
			image	= ImageChops.invert(image)

		# landscape -> native portrait orientation
		image	= image.rotate((self.rotate * 90 + 90) % 360, expand=True)

		if image.size != (self.PANEL_WIDTH, self.PANEL_HEIGHT):
			image	= image.resize((self.PANEL_WIDTH, self.PANEL_HEIGHT))

		return(image.point(lambda p: 255 if p >= 128 else 0, mode='1'))

	def __update(self, panel_image):
		if self.__shown is not None and ImageChops.difference(panel_image.convert('L'), self.__shown.convert('L')).getbbox() is None:
			return()

		buffer	= list(panel_image.tobytes())

		full	= (
			self.__shown is None or
			self.__partial_count >= self.FULL_REFRESH_EVERY or
			time.time() - self.__last_full >= self.FULL_REFRESH_SEC
		)

		if full:
			self.__init_panel()
			self.__write_ram(0x24, buffer)
			self.__write_ram(0x26, buffer)
			self.__turn_on(0xF7)
			self.__partial_count	= 0
			self.__last_full		= time.time()
		else:
			if not self.__awake:
				# RAM is lost in deep sleep: restore the image on screen as reference for the partial refresh
				self.__init_panel()
				self.__write_ram(0x26, list(self.__shown.tobytes()))
			self.__partial(buffer)
			self.__partial_count	+= 1

		self.__shown		= panel_image
		self.__last_update	= time.time()

	# panel commands

	def __reset(self, low_ms=2):
		GPIO.output(self.PIN_RST, 1)
		time.sleep(0.02)
		GPIO.output(self.PIN_RST, 0)
		time.sleep(low_ms / 1000)
		GPIO.output(self.PIN_RST, 1)
		time.sleep(0.02)

	def __command(self, command, data=None):
		GPIO.output(self.PIN_DC, 0)
		self.__spi.writebytes([command])
		if data:
			GPIO.output(self.PIN_DC, 1)
			self.__spi.writebytes2(data)

	def __wait_busy(self):
		start	= time.time()
		while GPIO.input(self.PIN_BUSY) == 1:
			if time.time() - start > self.BUSY_TIMEOUT_SEC:
				raise TimeoutError('e-paper busy timeout')
			time.sleep(0.01)

	def __set_window(self):
		x_end	= self.PANEL_WIDTH - 1
		y_end	= self.PANEL_HEIGHT - 1
		self.__command(0x44, [0x00, (x_end >> 3) & 0xFF])
		self.__command(0x45, [0x00, 0x00, y_end & 0xFF, (y_end >> 8) & 0xFF])
		self.__command(0x4E, [0x00])
		self.__command(0x4F, [0x00, 0x00])

	def __init_panel(self):
		self.__reset()
		self.__wait_busy()
		self.__command(0x12)					# software reset
		self.__wait_busy()
		self.__command(0x01, [0xF9, 0x00, 0x00])	# driver output control
		self.__command(0x11, [0x03])			# data entry mode
		self.__set_window()
		self.__command(0x3C, [0x05])			# border waveform
		self.__command(0x21, [0x00, 0x80])		# display update control
		self.__command(0x18, [0x80])			# internal temperature sensor
		self.__wait_busy()
		self.__awake	= True

	def __write_ram(self, register, buffer):
		self.__command(register, buffer)

	def __turn_on(self, mode):
		self.__command(0x22, [mode])
		self.__command(0x20)
		self.__wait_busy()

	def __partial(self, buffer):
		GPIO.output(self.PIN_RST, 0)
		time.sleep(0.001)
		GPIO.output(self.PIN_RST, 1)
		self.__command(0x3C, [0x80])
		self.__command(0x01, [0xF9, 0x00, 0x00])
		self.__command(0x11, [0x03])
		self.__set_window()
		self.__write_ram(0x24, buffer)
		self.__turn_on(0xFF)

	def __sleep(self):
		self.__command(0x10, [0x01])			# deep sleep
		time.sleep(0.1)
		self.__awake	= False
