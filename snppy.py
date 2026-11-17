#!/usr/bin/env python3
"""Snppy - SNES emulator frontend (Python / Tkinter) with on-screen buttons.

Works on PC (Windows/Linux/macOS) and Android (Pydroid 3, Termux + tkinter).
- Loads .sfc / .smc / .swc / .fig ROMs, detects LoROM/HiROM, parses the header.
- On-screen SNES pad (D-pad, A B X Y, L R, Start, Select): mouse, touch and keyboard.
- Produces the SNES joypad auto-read value ($4218/$4219) every frame.
- CPU/PPU core is pluggable: put a module `snes_core.py` next to this file with
      class Core:
          def __init__(self, rom: bytes, mapping: str): ...
          def set_joypad(self, port: int, bits: int): ...
          def run_frame(self) -> bytes   # 256*224*3 RGB bytes
  If no core is found, Snppy shows the ROM info + pad test screen.

Keyboard: arrows = D-pad, Z=B, X=A, A=Y, S=X, Q=L, W=R, Enter=Start, Shift=Select
"""
import os
import sys
import time
import tkinter as tk
from tkinter import filedialog

W, H, SCALE = 256, 224, 2
FPS = 60

# joypad bit layout of the 16-bit auto-read value
BITS = {"B": 15, "Y": 14, "SELECT": 13, "START": 12, "UP": 11, "DOWN": 10,
        "LEFT": 9, "RIGHT": 8, "A": 7, "X": 6, "L": 5, "R": 4}

KEYS = {"Up": "UP", "Down": "DOWN", "Left": "LEFT", "Right": "RIGHT",
        "z": "B", "x": "A", "a": "Y", "s": "X", "q": "L", "w": "R",
        "Return": "START", "Shift_L": "SELECT", "Shift_R": "SELECT"}


class Rom:
    def __init__(self, data: bytes, path: str):
        self.path = path
        if len(data) % 1024 == 512:          # strip copier header
            data = data[512:]
        self.data = data
        self.mapping, self.hdr = self._find_header()
        h = self.hdr
        raw = data[h:h + 21]
        self.title = "".join(chr(c) if 32 <= c < 127 else "?" for c in raw).strip()
        self.map_mode = data[h + 21]
        self.rom_type = data[h + 22]
        self.rom_kb = 1 << data[h + 23] if data[h + 23] < 16 else 0
        self.sram_kb = (1 << data[h + 24]) if 0 < data[h + 24] <= 8 else 0
        self.checksum = data[h + 28] | data[h + 29] << 8
        self.complement = data[h + 30] | data[h + 31] << 8
        self.valid = (self.checksum ^ self.complement) == 0xFFFF
        self.reset = data[h + 0x3C] | data[h + 0x3D] << 8

    def _score(self, off, hirom=False):
        d = self.data
        if off + 0x40 > len(d):
            return -1
        s = 0
        mm = d[off + 21]
        if 0x20 <= mm <= 0x3F and (mm & 1) == (1 if hirom else 0):
            s += 3
        if d[off + 23] <= 0x0D:
            s += 1
        if d[off + 24] <= 0x08:
            s += 1
        cs = d[off + 28] | d[off + 29] << 8
        cc = d[off + 30] | d[off + 31] << 8
        if cs ^ cc == 0xFFFF:
            s += 4
        reset = d[off + 0x3C] | d[off + 0x3D] << 8
        if reset >= 0x8000:
            s += 2
        if all(32 <= c < 127 for c in d[off:off + 21]):
            s += 2
        return s

    def _find_header(self):
        lo, hi = self._score(0x7FC0, False), self._score(0xFFC0, True)
        return ("HiROM", 0xFFC0) if hi > lo else ("LoROM", 0x7FC0)

    def info(self):
        return [f"Title: {self.title}",
                f"Mapping: {self.mapping} (mode ${self.map_mode:02X})",
                f"Size: {len(self.data)//1024} KB  (header says {self.rom_kb} KB)",
                f"SRAM: {self.sram_kb} KB",
                f"Checksum: ${self.checksum:04X}  {'OK' if self.valid else 'mismatch'}",
                f"Reset vector: ${self.reset:04X}"]


def load_core(rom):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import snes_core
        return snes_core.Core(rom.data, rom.mapping)
    except Exception:
        return None


class Snppy:
    def __init__(self, root):
        self.root = root
        root.title("Snppy - SNES")
        root.configure(bg="#1b1b22")
        self.pad = 0
        self.held_keys = set()
        self.rom = None
        self.core = None
        self.bios = None
        self.frame = 0
        self.img = tk.PhotoImage(width=W, height=H)

        bar = tk.Frame(root, bg="#1b1b22")
        bar.pack(fill="x")
        tk.Button(bar, text="Open ROM", command=self.open_rom).pack(side="left", padx=4, pady=4)
        tk.Button(bar, text="Reset", command=self.reset).pack(side="left", padx=4)
        tk.Button(bar, text="Load BIOS", command=self.open_bios).pack(side="left", padx=4)
        self.status = tk.Label(bar, text="No ROM", fg="#9fe", bg="#1b1b22")
        self.status.pack(side="left", padx=8)

        self.screen = tk.Canvas(root, width=W * SCALE, height=H * SCALE, bg="black",
                                highlightthickness=0)
        self.screen.pack()
        self.screen_img = self.screen.create_image(0, 0, anchor="nw", image=self.img)

        self.build_pad()
        root.bind("<KeyPress>", self.key_down)
        root.bind("<KeyRelease>", self.key_up)
        self.draw_idle()
        self.last = time.time()
        self.tick()

    # ---------- on-screen pad ----------
    def build_pad(self):
        pad = tk.Canvas(self.root, width=W * SCALE, height=190, bg="#c9c9d1",
                        highlightthickness=0)
        pad.pack(pady=(6, 0))
        self.padc = pad
        self.btn_items = {}

        def add(name, x0, y0, x1, y1, label, color, oval=False):
            if oval:
                it = pad.create_oval(x0, y0, x1, y1, fill=color, outline="#444", width=2)
            else:
                it = pad.create_rectangle(x0, y0, x1, y1, fill=color, outline="#444", width=2)
            pad.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=label,
                            font=("Arial", 12, "bold"), fill="white", state="disabled")
            self.btn_items[it] = (name, color)
            pad.tag_bind(it, "<ButtonPress-1>", lambda e, n=name: self.press(n, True))
            pad.tag_bind(it, "<ButtonRelease-1>", lambda e, n=name: self.press(n, False))

        add("L", 20, 8, 150, 42, "L", "#777")
        add("R", 362, 8, 492, 42, "R", "#777")
        # D-pad
        add("UP", 70, 55, 110, 95, "▲", "#333")
        add("DOWN", 70, 135, 110, 175, "▼", "#333")
        add("LEFT", 30, 95, 70, 135, "◀", "#333")
        add("RIGHT", 110, 95, 150, 135, "▶", "#333")
        # face buttons
        add("X", 380, 55, 430, 105, "X", "#2a6fd6", True)
        add("Y", 330, 100, 380, 150, "Y", "#2ea043", True)
        add("A", 430, 100, 480, 150, "A", "#d62a2a", True)
        add("B", 380, 145, 430, 175, "B", "#e0b020", True)
        add("SELECT", 195, 130, 245, 152, "", "#555")
        add("START", 265, 130, 315, 152, "", "#555")
        # multi-touch friendly: release everything if pointer leaves
        pad.bind("<Leave>", lambda e: self.release_touch())
        self.touch = set()

    def release_touch(self):
        for n in list(self.touch):
            self.press(n, False)

    def press(self, name, down):
        if down:
            self.pad |= 1 << BITS[name]
            self.touch.add(name)
        else:
            if name in self.held_keys:
                return
            self.pad &= ~(1 << BITS[name])
            self.touch.discard(name)
        self.refresh_pad()

    def refresh_pad(self):
        for it, (name, color) in self.btn_items.items():
            on = self.pad >> BITS[name] & 1
            self.padc.itemconfig(it, fill="#fff36b" if on else color)
        if self.core:
            self.core.set_joypad(0, self.pad)

    # ---------- keyboard ----------
    def key_down(self, e):
        n = KEYS.get(e.keysym) or KEYS.get(e.keysym.lower())
        if n:
            self.held_keys.add(n)
            self.pad |= 1 << BITS[n]
            self.refresh_pad()

    def key_up(self, e):
        n = KEYS.get(e.keysym) or KEYS.get(e.keysym.lower())
        if n:
            self.held_keys.discard(n)
            self.pad &= ~(1 << BITS[n])
            self.refresh_pad()

    # ---------- ROM ----------
    def open_rom(self):
        p = filedialog.askopenfilename(
            title="SNES ROM",
            filetypes=[("SNES ROM", "*.sfc *.smc *.swc *.fig *.bin"), ("All", "*.*")])
        if p:
            self.load(p)

    def open_bios(self):
        p = filedialog.askopenfilename(
            title="BIOS / firmware file",
            filetypes=[("BIOS", "*.bin *.rom *.bios *.sfc"), ("All", "*.*")])
        if not p:
            return
        try:
            with open(p, "rb") as f:
                self.bios = f.read()
        except Exception as ex:
            self.status.config(text=f"BIOS error: {ex}")
            return
        if self.core and hasattr(self.core, "load_bios"):
            self.core.load_bios(self.bios)
        self.status.config(text=f"BIOS {len(self.bios)} B")

    def load(self, path):
        try:
            with open(path, "rb") as f:
                self.rom = Rom(f.read(), path)
        except Exception as ex:
            self.status.config(text=f"Error: {ex}")
            return
        self.reset()

    def reset(self):
        if not self.rom:
            return
        self.core = load_core(self.rom)
        self.frame = 0
        self.status.config(text=f"{self.rom.mapping}" + ("" if self.core else " - info only"))
        if self.core:
            if self.bios and hasattr(self.core, "load_bios"):
                self.core.load_bios(self.bios)
            self.core.set_joypad(0, self.pad)

    # ---------- display ----------
    def draw_idle(self):
        c = self.screen
        c.delete("info")
        c.create_rectangle(0, 0, W * SCALE, H * SCALE, fill="#101018", tags="info")
        lines = ["SNPPY - SNES", "", "Open a ROM (.sfc / .smc)"] if not self.rom else self.rom.info()
        y = 24
        for ln in lines:
            c.create_text(10, y, anchor="w", text=ln[:34], fill="#9fe",
                          font=("Courier", 7), tags="info")
            y += 20
        v = self.pad
        c.create_text(10, H * SCALE - 40, anchor="w", fill="#fff36b",
                      font=("Courier", 7), tags="info",
                      text=f"PAD ${(v >> 8) & 255:02X}{v & 255:02X}")
        c.create_text(10, H * SCALE - 20, anchor="w", fill="#888",
                      font=("Courier", 7), tags="info",
                      text="no core: snes_core.py missing" if self.rom else "")
        c.tag_raise("info")

    def tick(self):
        t = time.time()
        if self.core:
            try:
                rgb = self.core.run_frame()
                ppm = b"P6 %d %d 255\n" % (W, H) + bytes(rgb)
                self.img = tk.PhotoImage(data=ppm, format="PPM").zoom(SCALE)
                self.screen.delete("info")
                self.screen.itemconfig(self.screen_img, image=self.img)
            except Exception as ex:
                self.status.config(text=f"Core error: {ex}")
                self.core = None
        else:
            self.draw_idle()
        self.frame += 1
        delay = max(1, int(1000 / FPS - (time.time() - t) * 1000))
        self.root.after(delay, self.tick)


def main():
    root = tk.Tk()
    app = Snppy(root)
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        app.load(sys.argv[1])
    root.mainloop()


if __name__ == "__main__":
    main()
