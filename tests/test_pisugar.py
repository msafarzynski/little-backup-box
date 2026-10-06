import threading
import time

import pytest

import lib_pisugar


class FakeBus(object):
	# like the PiSugar 3: writes are ignored unless register 0x0B is unlocked with 0x29
	def __init__(self, *args):
		self.regs = [0] * 0x40
		self.regs[0x02] = 0xEC
		self.regs[0x03] = 0x40
		self.lock = threading.Lock()

	def read_i2c_block_data(self, address, register, length):
		with self.lock:
			return self.regs[register:register + length]

	def read_byte_data(self, address, register):
		with self.lock:
			return self.regs[register]

	def write_byte_data(self, address, register, value):
		with self.lock:
			if register == 0x0B or self.regs[0x0B] == 0x29:
				self.regs[register] = value & 0xFF

	def firmware_sets(self, register, value):
		with self.lock:
			self.regs[register] = value


@pytest.fixture
def buttons(monkeypatch):
	bus = FakeBus()
	monkeypatch.setattr(lib_pisugar.smbus2, 'SMBus', lambda *args: bus)
	events = []
	monkeypatch.setattr(lib_pisugar, 'BUTTON_MAP', {e: e for e in lib_pisugar.BUTTON_MAP})
	device = lib_pisugar.pisugar3_buttons({e: (lambda e=e: events.append(e)) for e in lib_pisugar.BUTTON_MAP})
	yield device, bus, events
	device.stop()


def press_sequence(device, steps):
	# steps: (pressed, time) for button 2, fed directly into the gesture detector
	device.stop()
	time.sleep(0.1)
	for pressed, t in steps:
		device._pisugar3_buttons__b2_step(pressed, t)


@pytest.mark.parametrize('tap, event', [(1, 'b1_single'), (2, 'b1_double'), (3, 'b1_long')])
def test_button1_tap_is_reported_and_cleared(buttons, tap, event):
	device, bus, events = buttons
	bus.firmware_sets(0x08, 0xF0 | tap)
	time.sleep(0.2)
	assert events == [event]
	assert bus.regs[0x08] == 0xF0	# only the tap bits are cleared


def test_button2_single(buttons):
	device, bus, events = buttons
	press_sequence(device, [(True, 0.0), (False, 0.1), (False, 0.3), (False, 0.55)])
	assert events == ['b2_single']


def test_button2_double(buttons):
	device, bus, events = buttons
	press_sequence(device, [(True, 0.0), (False, 0.1), (True, 0.25), (False, 0.35), (False, 1.0)])
	assert events == ['b2_double']


def test_button2_long_fires_while_held(buttons):
	device, bus, events = buttons
	press_sequence(device, [(True, 0.0), (True, 0.5), (True, 0.85)])
	assert events == ['b2_long']
	press_sequence(device, [(True, 2.0), (False, 3.0), (False, 4.0)])
	assert events == ['b2_long']


def test_button2_double_press_and_hold_is_ignored(buttons):
	# PiSugar power on/off gesture
	device, bus, events = buttons
	press_sequence(device, [(True, 0.0), (False, 0.1), (True, 0.25), (True, 1.2), (False, 3.0), (False, 4.0)])
	assert events == []


def test_button2_live_polling(buttons):
	device, bus, events = buttons
	bus.firmware_sets(0x02, 0xED)
	time.sleep(0.15)
	bus.firmware_sets(0x02, 0xEC)
	time.sleep(0.6)
	assert events == ['b2_single']


def test_i2c_errors_are_survived(buttons, monkeypatch):
	device, bus, events = buttons
	calls = {'n': 0}
	original = bus.read_i2c_block_data

	def flaky(*args):
		calls['n'] += 1
		if calls['n'] <= 3:
			raise TimeoutError(110, 'Connection timed out')
		return original(*args)

	monkeypatch.setattr(bus, 'read_i2c_block_data', flaky)
	bus.firmware_sets(0x08, 1)
	time.sleep(0.5)
	assert events == ['b1_single']


def test_unmapped_action_is_ignored(monkeypatch):
	bus = FakeBus()
	monkeypatch.setattr(lib_pisugar.smbus2, 'SMBus', lambda *args: bus)
	monkeypatch.setattr(lib_pisugar, 'BUTTON_MAP', {'b1_single': 'none'})
	device = lib_pisugar.pisugar3_buttons({'down': lambda: pytest.fail('must not fire')})
	bus.firmware_sets(0x08, 1)
	time.sleep(0.2)
	device.stop()
	assert bus.regs[0x08] == 0


def test_power_button_long_press_is_not_used():
	# long press is the PiSugar hardware power off
	assert lib_pisugar.BUTTON_MAP['b2_long'] == 'none'


def test_writes_are_locked_again(buttons):
	device, bus, events = buttons
	bus.firmware_sets(0x08, 1)
	time.sleep(0.2)
	assert bus.regs[0x08] == 0
	assert bus.regs[0x0B] == 0x00


def test_garbage_while_pisugar_restarts_is_ignored(buttons):
	# the controller returned all zeros and a stuck long tap while restarting
	device, bus, events = buttons
	bus.firmware_sets(0x02, 0x00)
	bus.firmware_sets(0x08, 0x03)
	time.sleep(0.2)
	assert events == []
	bus.firmware_sets(0x02, 0xEC)
	time.sleep(0.2)
	assert events == ['b1_long']


def test_tap_is_not_repeated_when_clearing_fails(buttons, monkeypatch):
	device, bus, events = buttons
	original = bus.write_byte_data
	failing = {'on': True}

	def write(address, register, value):
		if failing['on'] and register == 0x08:
			raise OSError(121, 'Remote I/O error')
		original(address, register, value)

	monkeypatch.setattr(bus, 'write_byte_data', write)
	bus.firmware_sets(0x08, 0x03)
	time.sleep(0.3)
	assert events == []
	failing['on'] = False
	time.sleep(0.3)
	assert events == ['b1_long']


@pytest.mark.parametrize('action, output_on', [('poweroff', False), ('halt', False), ('reboot', True), ('kexec', True)])
def test_system_shutdown_hook(monkeypatch, action, output_on):
	bus = FakeBus()
	monkeypatch.setattr(lib_pisugar.smbus2, 'SMBus', lambda *args: bus)
	lib_pisugar.system_shutdown(action)
	assert bool(bus.regs[0x02] & 0x20) == output_on
	assert bus.regs[0x02] | 0x20 == 0xEC	# other bits untouched
	assert bus.regs[0x0B] == 0x00
