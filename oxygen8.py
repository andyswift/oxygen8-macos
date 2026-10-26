#!/usr/bin/env python3
"""Userspace driver for the original M-Audio Oxygen 8 (USB 0763:1014) on macOS.

1. If the keyboard is in its blank state (0763:1014), upload the Keystation
   firmware to its EZ-USB (AN21xx) chip; it then re-enumerates as 0763:1015.
2. Read Midiman-format packets from it and publish them on a virtual CoreMIDI
   source named "Oxygen 8".

Midiman packet format (see Linux sound/usb/midi.c, snd_usbmidi_midiman_input):
4 bytes, b0..b2 = raw MIDI bytes, b3 = (cable << 4) | byte count.

The firmware is Midiman's and may only be redistributed inside Debian's midisport-firmware
package, so it is not included here: on first run it is downloaded from Debian (checksum
verified) and extracted next to this script.
"""
import argparse
import hashlib
import io
import os
import sys
import tarfile
import time
import urllib.request

import rtmidi
import usb.core
import usb.util

VENDOR = 0x0763
PID_BLANK = 0x1014
PID_READY = 0x1015
FIRMWARE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firmware", "MidiSportKS.ihx")
FIRMWARE_URL = "https://deb.debian.org/debian/pool/non-free/m/midisport-firmware/midisport-firmware_1.2.orig.tar.gz"
FIRMWARE_TARBALL_SHA256 = "79070472f80abaf5e953cdfc5270c5526f42530994defdcf70b80b8cca326562"
FIRMWARE_SHA256 = "2905341167bcca589c42851ec5f6aeed606cbfc8592400663a428ee376ea26e0"

EZUSB_RW_INTERNAL = 0xA0
AN21_CPUCS = 0x7F92
PORT_NAME = "Oxygen 8"


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


def ensure_firmware():
    """Download the Keystation firmware from Debian's midisport-firmware package if it isn't here yet."""
    if os.path.exists(FIRMWARE):
        return
    log("firmware not found, downloading it from Debian's midisport-firmware package")
    with urllib.request.urlopen(FIRMWARE_URL, timeout=30) as r:
        tarball = r.read()
    if hashlib.sha256(tarball).hexdigest() != FIRMWARE_TARBALL_SHA256:
        raise RuntimeError(f"checksum mismatch for {FIRMWARE_URL}")
    with tarfile.open(fileobj=io.BytesIO(tarball)) as tar:
        firmware = tar.extractfile("midisport-firmware-1.2.orig/MidiSportKS.ihx").read()
        license_text = tar.extractfile("midisport-firmware-1.2.orig/LICENSE").read()
    if hashlib.sha256(firmware).hexdigest() != FIRMWARE_SHA256:
        raise RuntimeError("checksum mismatch for MidiSportKS.ihx")
    os.makedirs(os.path.dirname(FIRMWARE), exist_ok=True)
    with open(FIRMWARE, "wb") as f:
        f.write(firmware)
    with open(os.path.join(os.path.dirname(FIRMWARE), "LICENSE"), "wb") as f:
        f.write(license_text)
    log(f"firmware saved to {FIRMWARE}")


def find(pid):
    return usb.core.find(idVendor=VENDOR, idProduct=pid)


def parse_ihx(path):
    """Yield (address, data) for each data record in an Intel HEX file."""
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line.startswith(":"):
                continue
            raw = bytes.fromhex(line[1:])
            count, addr, rtype = raw[0], (raw[1] << 8) | raw[2], raw[3]
            if sum(raw) & 0xFF:
                raise ValueError(f"bad checksum in {path}: {line}")
            if rtype == 0x00:
                yield addr, raw[4:4 + count]
            elif rtype == 0x01:
                return


def load_firmware(dev):
    def write(addr, data):
        dev.ctrl_transfer(0x40, EZUSB_RW_INTERNAL, addr, 0, data, timeout=1000)

    log("blank Oxygen 8 found, uploading firmware")
    write(AN21_CPUCS, b"\x01")  # hold 8051 in reset
    for addr, data in parse_ihx(FIRMWARE):
        write(addr, data)
    try:
        write(AN21_CPUCS, b"\x00")  # run; device disconnects and re-enumerates
    except usb.core.USBError:
        pass
    usb.util.dispose_resources(dev)


class MidiParser:
    """Reassemble a raw MIDI byte stream into complete messages."""

    def __init__(self):
        self.status = None
        self.buf = []
        self.sysex = None

    @staticmethod
    def _length(status):
        if status < 0xF0:
            return 2 if status & 0xF0 in (0xC0, 0xD0) else 3
        return {0xF1: 2, 0xF2: 3, 0xF3: 2}.get(status, 1)

    def feed(self, byte):
        """Return a complete message (list of ints) or None."""
        if byte >= 0xF8:  # realtime, may interleave anything
            return [byte]
        if self.sysex is not None:
            self.sysex.append(byte)
            if byte == 0xF7:
                msg, self.sysex = self.sysex, None
                return msg
            if byte & 0x80:  # sysex aborted by a new status byte
                self.sysex = None
            else:
                return None
        if byte == 0xF0:
            self.sysex, self.status = [byte], None
            return None
        if byte & 0x80:
            self.buf = [byte]
            self.status = byte if byte < 0xF0 else None  # system common cancels running status
        elif self.buf:
            self.buf.append(byte)
        elif self.status is not None:  # running status
            self.buf = [self.status, byte]
        else:
            return None  # stray data byte
        if len(self.buf) == self._length(self.buf[0]):
            msg, self.buf = self.buf, []
            return msg
        return None


def run(dev, midi_out, verbose):
    dev.set_configuration()
    intf = dev.get_active_configuration()[(0, 0)]
    ep_in = usb.util.find_descriptor(
        intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
    if ep_in is None:
        raise RuntimeError(f"no IN endpoint found:\n{dev}")
    usb.util.claim_interface(dev, intf)
    log(f"Oxygen 8 ready, reading endpoint 0x{ep_in.bEndpointAddress:02x}")

    parser = MidiParser()
    size = ep_in.wMaxPacketSize
    while True:
        try:
            data = ep_in.read(size, timeout=1000)
        except usb.core.USBTimeoutError:
            continue
        for i in range(0, len(data) - 3, 4):
            count = data[i + 3] & 0x03
            for b in data[i:i + count]:
                msg = parser.feed(b)
                if msg:
                    if verbose and msg != [0xFE]:  # skip active-sensing keepalives
                        log(" ".join(f"{x:02X}" for x in msg))
                    midi_out.send_message(msg)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true", help="print every MIDI message")
    args = ap.parse_args()
    ensure_firmware()

    midi_out = rtmidi.MidiOut(rtmidi.API_MACOSX_CORE)
    midi_out.open_virtual_port(PORT_NAME)
    log(f'virtual MIDI source "{PORT_NAME}" created, waiting for keyboard')

    waiting_logged = False
    while True:
        try:
            dev = find(PID_READY)
            if dev is not None:
                run(dev, midi_out, args.verbose)
            blank = find(PID_BLANK)
            if blank is not None:
                load_firmware(blank)
                # re-enumeration takes a few seconds; uploading again mid-way wedges the chip
                for _ in range(20):
                    time.sleep(0.5)
                    if find(PID_READY) is not None:
                        break
                else:
                    log("keyboard did not come back after firmware upload; try replugging it")
                continue
            if not waiting_logged:
                log("keyboard not connected")
                waiting_logged = True
        except usb.core.USBError as e:
            log(f"USB error: {e}; waiting for keyboard")
            waiting_logged = True
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
