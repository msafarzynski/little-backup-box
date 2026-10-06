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

# PiSugar 3 buttons as menu input (I2C bus 1, address 0x57).
#
# button 1: the firmware detects single/double/long taps and reports them in register 0x08 (bits 0-1).
#           The event is cleared after reading (as pisugar-server does).
# button 2: the PiSugar power button. Register 0x02 bit 0 only reflects the pressed state,
#           gestures are detected here. Long press (power off) and double press and hold (power on)
#           belong to the PiSugar and are not mapped.
#
# Do not run pisugar-server in parallel, it would consume the tap events of button 1.

import sys
import threading
import time

import smbus2

# event -> menu action (down, up, right, left or none)
BUTTON_MAP	= {
	'b1_single':	'down',
	'b1_double':	'up',
	'b1_long':		'right',
	'b2_single':	'left',
	'b2_double':	'right',
	'b2_long':		'none',
}

class pisugar3_buttons(object):

	I2C_BUS			= 1
	I2C_ADDRESS		= 0x57
	REG_CTRL1		= 0x02
	REG_TAP			= 0x08

	POLL_SEC		= 0.05
	LONG_SEC		= 0.8
	DOUBLE_SEC		= 0.4

	TAP_EVENTS		= {1: 'single', 2: 'double', 3: 'long'}

	def __init__(self, actions):
		# actions: dict menu action -> callable
		self.__actions	= actions
		self.__running	= True

		self.__bus		= smbus2.SMBus(self.I2C_BUS)

		# button 2 gesture state
		self.__b2_pressed		= False
		self.__b2_presses		= 0
		self.__b2_pressed_at	= 0
		self.__b2_released_at	= 0
		self.__b2_long_fired	= False

		self.__thread	= threading.Thread(target=self.__run, daemon=True)
		self.__thread.start()

	def stop(self):
		self.__running	= False

	def __fire(self, event):
		action	= BUTTON_MAP.get(event, 'none')
		if action in self.__actions:
			try:
				self.__actions[action]()
			except Exception as e:
				print(f'PiSugar button action {action} failed: {e}', file=sys.stderr)

	def __run(self):
		errors	= 0

		while self.__running:
			try:
				# registers 0x02..0x08 in one transfer
				regs	= self.__bus.read_i2c_block_data(self.I2C_ADDRESS, self.REG_CTRL1, self.REG_TAP - self.REG_CTRL1 + 1)
				errors	= 0
			except OSError:
				# the bcm2835 i2c controller occasionally times out with the PiSugar
				errors	+= 1
				if errors == 100:
					print('PiSugar 3 not responding on I2C', file=sys.stderr)
				time.sleep(min(errors, 20) * self.POLL_SEC)
				continue

			now	= time.time()

			# button 1
			tap	= regs[self.REG_TAP - self.REG_CTRL1]
			if tap & 0x03:
				try:
					self.__bus.write_byte_data(self.I2C_ADDRESS, self.REG_TAP, tap & ~0x03)
				except OSError:
					pass
				self.__fire(f'b1_{self.TAP_EVENTS[tap & 0x03]}')

			# button 2
			self.__b2_step(bool(regs[0] & 0x01), now)

			time.sleep(self.POLL_SEC)

	def __b2_step(self, pressed, now):
		if pressed and not self.__b2_pressed:
			self.__b2_presses		+= 1
			self.__b2_pressed_at	= now
			self.__b2_long_fired	= False

		elif pressed:
			if not self.__b2_long_fired and now - self.__b2_pressed_at >= self.LONG_SEC:
				self.__b2_long_fired	= True
				if self.__b2_presses == 1:
					self.__fire('b2_long')

		elif self.__b2_pressed:
			self.__b2_released_at	= now
			if self.__b2_long_fired:
				self.__b2_presses	= 0
			elif self.__b2_presses >= 2:
				self.__b2_presses	= 0
				self.__fire('b2_double')

		elif self.__b2_presses == 1 and now - self.__b2_released_at >= self.DOUBLE_SEC:
			self.__b2_presses	= 0
			self.__fire('b2_single')

		self.__b2_pressed	= pressed

if __name__ == "__main__":
	# test: print button events and their menu actions
	menu_actions	= dict(BUTTON_MAP)
	BUTTON_MAP.update({event: event for event in menu_actions})
	buttons	= pisugar3_buttons({event: (lambda e=event: print(f'{e} -> {menu_actions[e]}', flush=True)) for event in menu_actions})
	try:
		while True:
			time.sleep(1)
	except KeyboardInterrupt:
		buttons.stop()
