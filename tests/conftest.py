# Hardware fakes so lib_epaper and lib_pisugar can be tested off the Pi.

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


class FakeGPIO(types.ModuleType):
	BCM = 'BCM'
	OUT = 'OUT'
	IN = 'IN'

	def __init__(self):
		super().__init__('RPi.GPIO')
		self.levels = {}
		self.busy = 0

	def setmode(self, mode):
		pass

	def setwarnings(self, flag):
		pass

	def setup(self, pin, direction):
		self.levels.setdefault(pin, 0)

	def output(self, pin, level):
		self.levels[pin] = level

	def input(self, pin):
		return self.busy


class FakeSpiDev(object):
	DC_PIN = 25

	def __init__(self):
		self.commands = []	# [command, data]
		self.ram = {}

	def open(self, port, device):
		pass

	def close(self):
		pass

	def writebytes(self, data):
		assert GPIO.levels.get(self.DC_PIN) == 0
		self.commands.append([data[0], []])

	def writebytes2(self, data):
		assert GPIO.levels.get(self.DC_PIN) == 1
		self.commands[-1][1] = list(data)
		self.ram[self.commands[-1][0]] = list(data)

	def refresh_modes(self):
		# display update modes (0x22 data) in order: 0xF7 full, 0xFF partial
		return [data[0] for command, data in self.commands if command == 0x22]

	def count(self, command):
		return sum(1 for c, _ in self.commands if c == command)


GPIO = FakeGPIO()
rpi = types.ModuleType('RPi')
rpi.GPIO = GPIO
sys.modules['RPi'] = rpi
sys.modules['RPi.GPIO'] = GPIO

spidev = types.ModuleType('spidev')
spidev.SpiDev = FakeSpiDev
sys.modules['spidev'] = spidev
