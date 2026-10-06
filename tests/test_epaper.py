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
	# as display.py renders for e-paper: black (0) text on white (255) background
	image = Image.new('1', (250, 122), 255)
	ImageDraw.Draw(image).text((5, 5), text, fill=0)
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


def visible(ram):
	# the last byte of each 16 byte row holds 6 padding bits outside the 122 visible pixels
	return set(b | 0x3F if i % 16 == 15 else b for i, b in enumerate(ram))


def test_images_are_shown_as_they_are(epd):
	# 255 is white paper (RAM bit 1), 0 is black (RAM bit 0)
	update(epd, frame())
	assert visible(spi(epd).ram[0x24]) == {0xFF}
	update(epd, Image.new('1', (250, 122), 0))
	assert visible(spi(epd).ram[0x24]) == {0x00, 0x3F}


@pytest.mark.parametrize('rotate, panel_xy', [(0, (121, 0)), (2, (0, 249))])
def test_rotation(rotate, panel_xy):
	device = lib_epaper.epd2in13_v4()
	device.capabilities(rotate=rotate)
	try:
		image = frame()
		image.putpixel((0, 0), 0)	# black pixel top left of the landscape frame
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
