import time

import pytest
from PIL import Image, ImageDraw

import lib_epaper


@pytest.fixture
def epd():
	device = lib_epaper.epd2in13_v4()
	device.capabilities(width=128, height=64, rotate=0, mode='1')
	yield device
	device.cleanup()


def spi(device):
	return device._epd2in13_v4__spi


def update(device, image):
	# synchronous update, bypassing the worker thread
	device._epd2in13_v4__update(device._epd2in13_v4__to_panel(image))


def frame(text=''):
	# as display.py renders in mode '1': white (255) text on black (0) background
	image = Image.new('1', (250, 122), 0)
	ImageDraw.Draw(image).text((5, 5), text, fill=255)
	return image


def test_fixed_landscape_resolution(epd):
	assert (epd.width, epd.height) == (250, 122)
	epd.capabilities(width=128, height=64, rotate=1, mode='1')
	assert (epd.width, epd.height) == (122, 250)


def test_first_frame_is_full_refresh_into_both_rams(epd):
	update(epd, frame('hello'))
	assert spi(epd).refresh_modes() == [0xF7]
	assert spi(epd).ram[0x24] == spi(epd).ram[0x26]
	assert len(spi(epd).ram[0x24]) == 16 * 250


def test_background_is_white_unless_inverse():
	normal = lib_epaper.epd2in13_v4(inverse=False)
	inverse = lib_epaper.epd2in13_v4(inverse=True)
	try:
		update(normal, frame())
		update(inverse, frame())
		# the last byte of each 16 byte row holds 6 padding bits outside the 122 visible pixels
		visible = lambda ram: [b & (0xC0 if i % 16 == 15 else 0xFF) for i, b in enumerate(ram)]
		assert set(visible(spi(normal).ram[0x24])) == {0xFF, 0xC0}
		assert set(visible(spi(inverse).ram[0x24])) == {0x00}
	finally:
		normal.cleanup()
		inverse.cleanup()


@pytest.mark.parametrize('rotate, panel_xy', [(0, (121, 0)), (2, (0, 249))])
def test_rotation(rotate, panel_xy):
	device = lib_epaper.epd2in13_v4()
	device.capabilities(rotate=rotate)
	try:
		image = frame()
		image.putpixel((0, 0), 255)	# top left of the landscape frame, black on the panel
		panel = device._epd2in13_v4__to_panel(image)
		assert panel.size == (122, 250)
		assert panel.getpixel(panel_xy) == 0
		assert panel.histogram()[0] == 1
	finally:
		device.cleanup()


def test_unchanged_frame_is_skipped(epd):
	update(epd, frame('a'))
	commands = len(spi(epd).commands)
	update(epd, frame('a'))
	assert len(spi(epd).commands) == commands


def test_changes_use_partial_refresh_and_periodic_full_refresh(epd):
	update(epd, frame('start'))
	for i in range(epd.FULL_REFRESH_EVERY):
		update(epd, frame(str(i)))
	assert spi(epd).refresh_modes() == [0xF7] + [0xFF] * epd.FULL_REFRESH_EVERY

	update(epd, frame('one more'))
	assert spi(epd).refresh_modes()[-1] == 0xF7


def test_full_refresh_after_time(epd):
	update(epd, frame('start'))
	epd._epd2in13_v4__last_full -= epd.FULL_REFRESH_SEC
	update(epd, frame('later'))
	assert spi(epd).refresh_modes() == [0xF7, 0xF7]


def test_wake_from_sleep_restores_reference_image(epd):
	update(epd, frame('before'))
	shown = spi(epd).ram[0x24]
	epd._epd2in13_v4__sleep()
	spi(epd).commands.clear()

	update(epd, frame('after'))
	assert spi(epd).count(0x12) == 1			# re-initialised
	assert spi(epd).refresh_modes() == [0xFF]	# no flashing full refresh
	assert spi(epd).ram[0x26] == shown


def test_worker_draws_latest_frame_and_sleeps_when_idle(monkeypatch):
	monkeypatch.setattr(lib_epaper.epd2in13_v4, 'SLEEP_AFTER_SEC', 0.2)
	device = lib_epaper.epd2in13_v4()
	try:
		device.display(frame('x'))
		deadline = time.time() + 2
		while 0x10 not in [c for c, _ in spi(device).commands] and time.time() < deadline:
			time.sleep(0.02)
		assert spi(device).refresh_modes() == [0xF7]
		assert spi(device).commands[-1] == [0x10, [0x01]]
	finally:
		device.cleanup()


def test_busy_timeout_does_not_kill_worker(monkeypatch):
	from conftest import GPIO
	monkeypatch.setattr(lib_epaper.epd2in13_v4, 'BUSY_TIMEOUT_SEC', 0.05)
	device = lib_epaper.epd2in13_v4()
	try:
		GPIO.busy = 1
		device.display(frame('stuck'))
		time.sleep(0.3)
		GPIO.busy = 0
		device.display(frame('recovered'))
		time.sleep(0.3)
		assert 0xF7 in spi(device).refresh_modes()
	finally:
		GPIO.busy = 0
		device.cleanup()
