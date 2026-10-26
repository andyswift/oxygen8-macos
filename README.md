# oxygen8-macos

A small userspace driver that makes the **original M-Audio Oxygen 8** USB keyboard work on modern
macOS, including Apple Silicon Macs. M-Audio's driver was a kernel extension that never came to
Apple Silicon or current macOS, so without it the keyboard shows up on USB and does nothing.

It appears in your DAW (Logic, GarageBand, Ableton, a browser with Web MIDI...) as a MIDI input
called **Oxygen 8**: keys with velocity, pitch bend, mod wheel and the knobs.

## Is this my keyboard?

This is for the first-generation Oxygen 8, which shows up on USB as `0763:1014` with no name:

```sh
ioreg -p IOUSB -l -w0 | grep -B3 -A3 '"idVendor" = 1891'   # look for "idProduct" = 4116
```

Later Oxygen models (Oxygen 8 v2, Oxygen 25/49/61...) are class compliant and work without this.

## Setup

You need [Homebrew](https://brew.sh) and Python 3.9 or newer.

```sh
brew install libusb
git clone https://github.com/andyswift/oxygen8-macos.git
cd oxygen8-macos
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```sh
.venv/bin/python oxygen8.py        # -v prints every MIDI message
```

Leave it running while you play and pick **Oxygen 8** as the MIDI input in your software. It copes
with unplugging and replugging the keyboard. Stop it with Ctrl-C.

On the first run it downloads the keyboard's firmware (see below) into `firmware/`.

## How it works

The original Oxygen 8 has a Cypress EZ-USB (AN21xx) chip with no firmware of its own: a driver has
to upload it every time the keyboard is plugged in. The driver:

1. Finds the blank keyboard (`0763:1014`) and uploads the Keystation firmware into the chip's RAM
   with EZ-USB vendor requests (hold the 8051 in reset, write the Intel HEX records, release it).
2. Waits for the keyboard to re-enumerate with its firmware running (`0763:1015`).
3. Reads its interrupt endpoint and decodes Midiman's 4-byte MIDI packets (three MIDI bytes plus a
   cable/length byte), reassembles them into MIDI messages (running status, SysEx, realtime) and
   sends them to a virtual CoreMIDI source via [python-rtmidi](https://github.com/SpotlightKid/python-rtmidi).

Everything runs in userspace with [pyusb](https://github.com/pyusb/pyusb) and libusb: no kernel
extension, no system changes, no `sudo`.

## The firmware

The firmware (`MidiSportKS.ihx`) is copyrighted by Midiman and may only be redistributed as part of
Debian's [`midisport-firmware`](https://packages.debian.org/source/sid/midisport-firmware) package,
so it is **not included in this repository**. On first run the driver downloads that package's
source tarball from `deb.debian.org`, checks its SHA-256 and extracts the firmware and its licence
into `firmware/`. To do it by hand, put `MidiSportKS.ihx` from that package in `firmware/`.

The approach and the Midiman protocol come from the Linux side: the `midisport-firmware` udev rules
(Clemens Ladisch) and the Midiman quirk in the kernel's `sound/usb/midi.c`.

## Troubleshooting

- **Nothing after "uploading firmware"**: the chip can get stuck half-way through re-enumerating.
  Unplug the keyboard, wait a couple of seconds, plug it back in.
- **Your software sees the notes but no sound**: check the instrument's MIDI channel. The Oxygen 8
  sends on channel 1 unless you change it on the keyboard.
- **`No backend available`**: pyusb can't find libusb. Run `brew install libusb`.
- **The keyboard is powered by USB only**: that is fine; a 9 V adapter is not needed.

## Limitations

- MIDI goes from the keyboard to the Mac only, not back (the Oxygen 8 makes no sound of its own).
- The driver only works while it is running. To start it at login, make a LaunchAgent that runs
  `.venv/bin/python oxygen8.py`.
- Tested on an Apple Silicon Mac running macOS 26 with one Oxygen 8.

## License

MIT for the code in this repository, see [LICENSE](LICENSE). The downloaded firmware keeps its own
licence (`firmware/LICENSE`).
