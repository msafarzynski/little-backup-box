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
#           gestures are detected here. Double press and hold (power on) belongs to the PiSugar.
#           Long press: soft power off is enabled while the menu runs (register 0x03 bit 4), the PiSugar
#           then only sets a flag (bit 3) instead of cutting the power and the box is shut down cleanly.
#           Soft power off is disabled again when the menu stops and by the system shutdown hook,
#           so the hard power off works whenever LBB is not running.
#
# System shutdown hook (/usr/lib/systemd/system-shutdown/): lib_pisugar.py --system-shutdown poweroff|halt|reboot
# disables soft power off and, on poweroff/halt, switches the PiSugar output off after the filesystems are read only.
#
# Register layout and write protection as in pisugar-server (pisugar-core/src/pisugar3.rs).
# Do not run pisugar-server in parallel, it would consume the tap events of button 1.

import argparse
import signal
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

I2C_BUS				= 1
I2C_ADDRESS			= 0x57

REG_CTRL1			= 0x02	# bit 5: power output enabled, bit 0: power button pressed
REG_CTRL2			= 0x03	# bit 4: soft power off enabled, bit 3: soft power off requested
REG_TAP				= 0x08	# bits 0-1: button 1 tap event
REG_WRITE_ENABLE	= 0x0B

WRITE_ENABLE_KEY	= 0x29

def write_register(bus, register, value):
	# the PiSugar 3 ignores writes unless they are unlocked
	bus.write_byte_data(I2C_ADDRESS, REG_WRITE_ENABLE, WRITE_ENABLE_KEY)
	try:
		bus.write_byte_data(I2C_ADDRESS, register, value)
	finally:
		bus.write_byte_data(I2C_ADDRESS, REG_WRITE_ENABLE, 0x00)

def set_soft_poweroff(bus, enable):
	ctrl2	= bus.read_byte_data(I2C_ADDRESS, REG_CTRL2) & 0b1110_0000
	write_register(bus, REG_CTRL2, ctrl2 | (0b0001_0000 if enable else 0))

def system_shutdown(action):
	bus	= smbus2.SMBus(I2C_BUS)
	set_soft_poweroff(bus, False)
	if action in ('poweroff', 'halt'):
		ctrl1	= bus.read_byte_data(I2C_ADDRESS, REG_CTRL1)
		write_register(bus, REG_CTRL1, ctrl1 & ~0b0010_0000)

class pisugar3_buttons(object):

	POLL_SEC		= 0.05
	LONG_SEC		= 0.8
	DOUBLE_SEC		= 0.4

	TAP_EVENTS		= {1: 'single', 2: 'double', 3: 'long'}

	def __init__(self, actions, on_poweroff=None):
		# actions: dict menu action -> callable
		# on_poweroff: callable for a long press of the power button, None keeps the PiSugar hard power off
		self.__actions		= actions
		self.__on_poweroff	= on_poweroff
		self.__running		= True

		self.__bus		= smbus2.SMBus(I2C_BUS)
		self.__set_soft_poweroff(on_poweroff is not None)

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
		self.__thread.join(timeout=1)
		self.__set_soft_poweroff(False)

	def __set_soft_poweroff(self, enable):
		for attempt in range(5):
			try:
				set_soft_poweroff(self.__bus, enable)
				return()
			except OSError:
				time.sleep(0.05)
		print(f'PiSugar 3 soft power off could not be {"enabled" if enable else "disabled"}', file=sys.stderr)

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
				regs	= self.__bus.read_i2c_block_data(I2C_ADDRESS, REG_CTRL1, REG_TAP - REG_CTRL1 + 1)
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
			tap	= regs[REG_TAP - REG_CTRL1]
			if tap & 0x03:
				try:
					write_register(self.__bus, REG_TAP, tap & ~0x03)
				except OSError:
					pass
				self.__fire(f'b1_{self.TAP_EVENTS[tap & 0x03]}')

			# power button long press
			ctrl2	= regs[REG_CTRL2 - REG_CTRL1]
			if self.__on_poweroff and (ctrl2 & 0b0001_1000) == 0b0001_1000:
				try:
					write_register(self.__bus, REG_CTRL2, ctrl2 & ~0b0000_1000)
				except OSError:
					continue
				on_poweroff, self.__on_poweroff	= self.__on_poweroff, None	# only once
				on_poweroff()

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
	parser	= argparse.ArgumentParser(description='PiSugar 3 buttons')
	parser.add_argument('--system-shutdown', choices=['poweroff', 'halt', 'reboot', 'kexec'], help='called by the systemd shutdown hook')
	parser.add_argument('--soft-poweroff', action='store_true', help='test mode: enable soft power off')
	args	= parser.parse_args()

	if args.system_shutdown:
		system_shutdown(args.system_shutdown)
		sys.exit()

	# test: print button events and their menu actions
	signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit())
	menu_actions	= dict(BUTTON_MAP)
	BUTTON_MAP.update({event: event for event in menu_actions})
	buttons	= pisugar3_buttons(
		{event: (lambda e=event: print(f'{e} -> {menu_actions[e]}', flush=True)) for event in menu_actions},
		on_poweroff=(lambda: print('power button long press -> poweroff', flush=True)) if args.soft_poweroff else None
	)
	try:
		while True:
			time.sleep(1)
	except KeyboardInterrupt:
		pass
	finally:
		buttons.stop()
