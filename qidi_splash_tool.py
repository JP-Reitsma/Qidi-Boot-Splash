"""Windows/Linux GUI wizard for reading and replacing the QIDI Q2 boot splash."""

from __future__ import annotations

import hashlib
import ipaddress
import io
import shlex
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

P5_SIZE = 9_437_184
WIDTH = 480
HEIGHT = 272
BMP_OFFSETS = (0x00647000, 0x006A6C00)
SLOT_INFO = {
    BMP_OFFSETS[0]: ("Brief startup splash", "about 1 second"),
    BMP_OFFSETS[1]: ("Main boot splash", "the rest of boot"),
}
REMOTE_PARTITION = "/dev/mmcblk0p5"


def require_pillow():
    try:
        from PIL import Image, ImageTk
    except ImportError as exc:
        raise RuntimeError("Install the required packages first: python -m pip install -r requirements.txt") from exc
    return Image, ImageTk


def bmp_image(data: bytes, offset: int):
    Image, _ = require_pillow()
    if data[offset:offset + 2] != b"BM":
        raise ValueError(f"No BMP found at 0x{offset:06X}")
    file_size = int.from_bytes(data[offset + 2:offset + 6], "little")
    pixel_offset = int.from_bytes(data[offset + 10:offset + 14], "little")
    dib_size = int.from_bytes(data[offset + 14:offset + 18], "little")
    width = int.from_bytes(data[offset + 18:offset + 22], "little", signed=True)
    height = int.from_bytes(data[offset + 22:offset + 26], "little", signed=True)
    planes = int.from_bytes(data[offset + 26:offset + 28], "little")
    bpp = int.from_bytes(data[offset + 28:offset + 30], "little")
    compression = int.from_bytes(data[offset + 30:offset + 34], "little")
    if (dib_size, width, abs(height), planes, bpp, compression) != (40, WIDTH, HEIGHT, 1, 24, 0):
        raise ValueError(
            f"Unexpected BMP at 0x{offset:06X}: {width}x{height}, {bpp} bpp, "
            f"DIB={dib_size}, planes={planes}, compression={compression}"
        )
    if file_size < pixel_offset or offset + file_size > len(data):
        raise ValueError(f"Invalid BMP length at 0x{offset:06X}")
    return Image.open(io.BytesIO(data[offset:offset + file_size])).convert("RGB")


def fit_artwork(path: Path):
    Image, _ = require_pillow()
    image = Image.open(path).convert("RGB")
    ratio = WIDTH / HEIGHT
    source_ratio = image.width / image.height
    if source_ratio > ratio:
        crop_width = round(image.height * ratio)
        left = (image.width - crop_width) // 2
        image = image.crop((left, 0, left + crop_width, image.height))
    else:
        crop_height = round(image.width / ratio)
        top = (image.height - crop_height) // 2
        image = image.crop((0, top, image.width, top + crop_height))
    return image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)


def patch_bmp(data: bytearray, offset: int, image) -> None:
    if data[offset:offset + 2] != b"BM":
        raise ValueError(f"No BMP found at 0x{offset:06X}")
    pixel_offset = int.from_bytes(data[offset + 10:offset + 14], "little")
    height = int.from_bytes(data[offset + 22:offset + 26], "little", signed=True)
    if pixel_offset != 54:
        raise ValueError(f"Unexpected BMP pixel offset at 0x{offset:06X}")
    pixels = bytearray()
    y_order = range(HEIGHT) if height < 0 else range(HEIGHT - 1, -1, -1)
    for y in y_order:
        for x in range(WIDTH):
            red, green, blue = image.getpixel((x, y))
            pixels.extend((blue, green, red))
    file_size = int.from_bytes(data[offset + 2:offset + 6], "little")
    if len(pixels) != file_size - pixel_offset:
        raise ValueError(f"Pixel data size mismatch at 0x{offset:06X}")
    start = offset + pixel_offset
    data[start:start + len(pixels)] = pixels


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class QidiSplashTool:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("QIDI Q2 Splash Tool")
        self.root.geometry("1160x800")
        self.root.minsize(1020, 700)
        self.root.configure(bg="#eef2f6")
        self.source_path: Path | None = None
        self.source_bytes: bytes | None = None
        self.originals = {}
        self.art_paths = {BMP_OFFSETS[0]: None, BMP_OFFSETS[1]: None}
        self.art_images = {BMP_OFFSETS[0]: None, BMP_OFFSETS[1]: None}
        self.patched_path: Path | None = None
        self.busy = False
        self.trusted_keys = {}
        self._photo_refs = []

        self.ip_var = tk.StringVar()
        self.user_var = tk.StringVar(value="mks")
        self.password_var = tk.StringVar()
        self.source_var = tk.StringVar(value="No p5.img loaded")
        self.output_var = tk.StringVar(value="No patched image created yet")
        self.same_art_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Choose a p5.img or download it from your Q2.")

        self._style()
        self._build()

    def _style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#eef2f6")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("TLabel", background="#eef2f6", foreground="#17212b", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 22, "bold"), foreground="#142b42")
        style.configure("Subtitle.TLabel", font=("Segoe UI", 10), foreground="#4b5f70")
        style.configure("CardTitle.TLabel", background="#ffffff", font=("Segoe UI", 13, "bold"), foreground="#17324b")
        style.configure("Small.TLabel", font=("Segoe UI", 9), foreground="#526475")
        style.configure("TButton", padding=(12, 7), font=("Segoe UI", 10))
        style.configure("Accent.TButton", padding=(14, 9), font=("Segoe UI", 10, "bold"))
        style.configure("TNotebook", background="#eef2f6", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 10), font=("Segoe UI", 10, "bold"))

    def _build(self):
        outer = ttk.Frame(self.root, padding=(22, 16))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="QIDI Q2 Splash Tool", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="One p5 partition image contains two splash BMPs: the brief startup image and the main boot image.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 14))
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.load_tab = ttk.Frame(self.notebook, padding=16)
        self.images_tab = ttk.Frame(self.notebook, padding=16)
        self.flash_tab = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(self.load_tab, text="1  Load p5 image")
        self.notebook.add(self.images_tab, text="2  Choose and preview")
        self.notebook.add(self.flash_tab, text="3  Install and verify")
        self._build_load_tab()
        self._build_images_tab()
        self._build_flash_tab()
        ttk.Label(outer, textvariable=self.status_var, relief="sunken", anchor="w", padding=(8, 6)).pack(fill="x", pady=(12, 0))

    def _connection_fields(self, parent):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", padx=12, pady=(10, 8))
        ttk.Label(row, text="Printer IP", style="Small.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(row, text="SSH user", style="Small.TLabel").grid(row=0, column=1, sticky="w", padx=(14, 0))
        ttk.Label(row, text="SSH password (used only in memory)", style="Small.TLabel").grid(row=0, column=2, sticky="w", padx=(14, 0))
        ttk.Entry(row, textvariable=self.ip_var, width=22).grid(row=1, column=0, sticky="ew")
        ttk.Entry(row, textvariable=self.user_var, width=14).grid(row=1, column=1, sticky="ew", padx=(14, 0))
        ttk.Entry(row, textvariable=self.password_var, width=24, show="●").grid(row=1, column=2, sticky="ew", padx=(14, 0))
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)
        row.columnconfigure(2, weight=2)

    def _build_load_tab(self):
        card = ttk.Frame(self.load_tab, style="Card.TFrame", padding=18)
        card.pack(fill="x", pady=(0, 12))
        ttk.Label(card, text="Get the current p5 partition image", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(
            card,
            text="The p5.img file is one 9 MiB partition image. It contains both embedded BMPs; the tool extracts them for preview.",
            style="Small.TLabel", wraplength=980,
        ).pack(anchor="w", pady=(5, 0))
        self._connection_fields(card)
        buttons = ttk.Frame(card, style="Card.TFrame")
        buttons.pack(fill="x", padx=12, pady=(4, 0))
        ttk.Button(buttons, text="Download p5.img from printer", command=self.download_partition).pack(side="left")
        ttk.Button(buttons, text="Choose a local p5.img", command=self.choose_local_partition).pack(side="left", padx=(10, 0))
        ttk.Label(card, textvariable=self.source_var, style="Small.TLabel", wraplength=1000).pack(anchor="w", padx=12, pady=(12, 0))
        ttk.Label(
            self.load_tab,
            text="Only the tested Q2 partition layout is accepted. Unknown offsets, image sizes, or BMP formats stop the workflow before any flash.",
            style="Small.TLabel", wraplength=1000,
        ).pack(anchor="w", pady=6)
        ttk.Button(self.load_tab, text="Continue to image previews →", command=lambda: self.notebook.select(self.images_tab)).pack(anchor="e", pady=8)

    def _build_images_tab(self):
        ttk.Label(self.images_tab, text="The two images have different jobs on the tested Q2", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(
            self.images_tab,
            text="Choose artwork for each slot. To avoid seeing an old image briefly, use the same artwork in both slots (default).",
            style="Small.TLabel", wraplength=1000,
        ).pack(anchor="w", pady=(4, 10))
        cards = ttk.Frame(self.images_tab)
        cards.pack(fill="both", expand=True)
        self.preview_widgets = {}
        for col, offset in enumerate(BMP_OFFSETS):
            title, duration = SLOT_INFO[offset]
            card = ttk.Frame(cards, style="Card.TFrame", padding=12)
            card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 8, 8 if col == 0 else 0))
            ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(card, text=f"0x{offset:06X} · shown {duration}", style="Small.TLabel").pack(anchor="w", pady=(2, 8))
            img_row = ttk.Frame(card, style="Card.TFrame")
            img_row.pack(fill="x")
            original_box = ttk.Frame(img_row, style="Card.TFrame")
            original_box.pack(side="left", expand=True, fill="x", padx=(0, 4))
            new_box = ttk.Frame(img_row, style="Card.TFrame")
            new_box.pack(side="left", expand=True, fill="x", padx=(4, 0))
            ttk.Label(original_box, text="Current", style="Small.TLabel").pack(anchor="w")
            current = ttk.Label(original_box, text="Load p5.img first", background="#17212b", foreground="#ffffff", anchor="center")
            current.pack(fill="both", pady=(3, 8))
            ttk.Label(new_box, text="New", style="Small.TLabel").pack(anchor="w")
            new = ttk.Label(new_box, text="Choose artwork", background="#17212b", foreground="#ffffff", anchor="center")
            new.pack(fill="both", pady=(3, 8))
            choose = ttk.Button(card, text="Choose artwork…", command=lambda o=offset: self.choose_artwork(o))
            choose.pack(anchor="w")
            self.preview_widgets[offset] = (current, new, choose)
        cards.columnconfigure(0, weight=1)
        cards.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            self.images_tab, text="Use the same new artwork in both slots", variable=self.same_art_var,
            command=self.toggle_same_art,
        ).pack(anchor="w", pady=(10, 0))
        actions = ttk.Frame(self.images_tab)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="Save extracted BMP previews…", command=self.save_extracted_previews).pack(side="left")
        ttk.Button(actions, text="Create patched p5.img…", style="Accent.TButton", command=self.create_patched_image).pack(side="right")
        self._refresh_previews()

    def _build_flash_tab(self):
        card = ttk.Frame(self.flash_tab, style="Card.TFrame", padding=18)
        card.pack(fill="x", pady=(0, 12))
        ttk.Label(card, text="Back up, flash, and verify", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(
            card,
            text="One click backs up /dev/mmcblk0p5, checks the image and backup, flashes, verifies the read-back, then restarts and reconnects to the printer.",
            style="Small.TLabel", wraplength=980,
        ).pack(anchor="w", pady=(5, 10))
        self._connection_fields(card)
        ttk.Label(card, textvariable=self.output_var, style="Small.TLabel", wraplength=1000).pack(anchor="w", padx=12, pady=(5, 10))
        self.flash_button = ttk.Button(card, text="Back up, flash & restart", style="Accent.TButton", command=self.flash_image)
        self.flash_button.pack(anchor="w", padx=12)
        self.log = tk.Text(self.flash_tab, height=12, wrap="word", background="#152333", foreground="#edf4fa", insertbackground="#ffffff", relief="flat", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, pady=(4, 0))
        self._log("Ready. The tool will not store your SSH password.")

    def _log(self, text: str):
        if hasattr(self, "log"):
            self.root.after(0, lambda: (self.log.insert("end", text + "\n"), self.log.see("end")))

    def _set_status(self, text: str):
        self.root.after(0, lambda: self.status_var.set(text))

    def ask_yes_no(self, title: str, text: str) -> bool:
        event = threading.Event()
        answer = {"value": False}
        def show():
            answer["value"] = messagebox.askyesno(title, text, parent=self.root)
            event.set()
        self.root.after(0, show)
        if not event.wait(timeout=300):
            return False
        return answer["value"]

    def _connection(self):
        address = self.ip_var.get().strip()
        try:
            ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError("Enter the printer's IP address, for example 192.168.1.40.") from exc
        username = self.user_var.get().strip()
        if not username or any(ch.isspace() for ch in username):
            raise ValueError("Enter the SSH user name (usually mks).")
        return address, username, self.password_var.get()

    def _connect(self, address: str, username: str, password: str):
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("Install the required packages first: python -m pip install -r requirements.txt") from exc
        app = self

        class AskHostKeyPolicy(paramiko.MissingHostKeyPolicy):
            """Accept an unknown SSH key only after showing its fingerprint."""

            def missing_host_key(self, client, hostname, key):
                import base64
                fingerprint = hashlib.sha256(key.asbytes()).digest()
                fingerprint_text = "SHA256:" + base64.b64encode(fingerprint).decode("ascii").rstrip("=")
                if not app.ask_yes_no(
                    "Confirm printer identity",
                    f"This printer's SSH key is not in your known-hosts list.\n\n"
                    f"Address: {hostname}\nKey type: {key.get_name()}\nFingerprint: {fingerprint_text}\n\n"
                    "Accept this key for this app session? Check with your printer administrator if unsure.",
                ):
                    raise RuntimeError("Printer SSH host key was not accepted")
                client.get_host_keys().add(hostname, key.get_name(), key)
                app.trusted_keys[(hostname, key.get_name())] = key

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        for (host, key_type), key in self.trusted_keys.items():
            client.get_host_keys().add(host, key_type, key)
        client.set_missing_host_key_policy(AskHostKeyPolicy())
        try:
            client.connect(address, username=username, password=password or None, timeout=12, banner_timeout=12, auth_timeout=20)
        except Exception:
            client.close()
            raise
        return client

    @staticmethod
    def _sudo(client, password: str, command: str) -> str:
        stdin, stdout, stderr = client.exec_command("sudo -S -p '' " + command, timeout=300)
        if password:
            stdin.write(password + "\n")
            stdin.flush()
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        status = stdout.channel.recv_exit_status()
        if status != 0:
            raise RuntimeError((err or out or f"Remote command failed with exit code {status}").strip())
        return out.strip()

    def _start_job(self, label, job, on_success=None):
        if self.busy:
            return
        self.busy = True
        self._set_status(label)
        def worker():
            try:
                result = job()
                self.root.after(0, lambda: self._job_done(label, result, on_success, None))
            except Exception as exc:
                self.root.after(0, lambda e=str(exc): self._job_done(label, None, None, e))
        threading.Thread(target=worker, daemon=True).start()

    def _job_done(self, label, result, on_success, error):
        self.busy = False
        if error:
            self._set_status("Stopped: " + error)
            self._log("ERROR: " + error)
            messagebox.showerror("QIDI Splash Tool", error, parent=self.root)
            return
        self._set_status(label + " — complete")
        if on_success:
            on_success(result)

    def _load_partition(self, path: Path):
        data = path.read_bytes()
        if len(data) != P5_SIZE:
            raise ValueError(f"Expected a 9 MiB p5 partition image ({P5_SIZE:,} bytes); found {len(data):,} bytes.")
        originals = {offset: bmp_image(data, offset) for offset in BMP_OFFSETS}
        self.source_path = path
        self.source_bytes = data
        self.originals = originals
        self.source_var.set(f"Loaded: {path}  ·  {len(data):,} bytes  ·  both Q2 BMPs validated")
        self._refresh_previews()
        self._log(f"Loaded and validated {path.name}. Found 480×272 BMPs at 0x647000 and 0x6A6C00.")

    def choose_local_partition(self):
        filename = filedialog.askopenfilename(title="Choose a fresh p5.img", filetypes=[("p5 image", "*.img"), ("All files", "*.*")])
        if not filename:
            return
        try:
            self._load_partition(Path(filename))
            self.status_var.set("p5.img loaded and both embedded BMPs validated.")
            self.notebook.select(self.images_tab)
        except Exception as exc:
            messagebox.showerror("Could not open p5.img", str(exc), parent=self.root)

    def download_partition(self):
        try:
            address, username, password = self._connection()
        except Exception as exc:
            messagebox.showerror("Connection details", str(exc), parent=self.root)
            return
        filename = filedialog.asksaveasfilename(
            title="Save the original p5 partition image",
            initialfile="p5_original.img",
            defaultextension=".img",
            filetypes=[("p5 image", "*.img")],
        )
        if not filename:
            return
        destination = Path(filename)
        remote = f"/home/{username}/qidi-p5-{uuid.uuid4().hex}.img"
        def job():
            self._log(f"Connecting to {username}@{address}…")
            client = self._connect(address, username, password)
            try:
                self._log("Reading /dev/mmcblk0p5 and creating a printer-side backup…")
                self._sudo(client, password, f"dd if={REMOTE_PARTITION} of={shlex.quote(remote)} bs=1M conv=fsync")
                with client.open_sftp() as sftp:
                    size = sftp.stat(remote).st_size
                    if size != P5_SIZE:
                        raise RuntimeError(f"The printer returned {size:,} bytes; expected {P5_SIZE:,}.")
                    sftp.get(remote, str(destination))
                data = destination.read_bytes()
                for offset in BMP_OFFSETS:
                    bmp_image(data, offset)
                return destination
            finally:
                client.close()
        def done(path):
            try:
                self._load_partition(path)
                self.notebook.select(self.images_tab)
            except Exception as exc:
                messagebox.showerror("Downloaded image is not compatible", str(exc), parent=self.root)
        self._start_job("Downloading and checking p5.img", job, done)

    def _load_artwork(self, path: Path):
        return fit_artwork(path)

    def choose_artwork(self, offset):
        filename = filedialog.askopenfilename(
            title="Choose splash artwork",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")],
        )
        if not filename:
            return
        path = Path(filename)
        try:
            image = self._load_artwork(path)
        except Exception as exc:
            messagebox.showerror("Could not open artwork", str(exc), parent=self.root)
            return
        self.art_paths[offset] = path
        self.art_images[offset] = image
        if self.same_art_var.get():
            other = BMP_OFFSETS[1] if offset == BMP_OFFSETS[0] else BMP_OFFSETS[0]
            self.art_paths[other] = path
            self.art_images[other] = image.copy()
        self._refresh_previews()
        self.status_var.set(f"Artwork loaded and previewed at 480×272: {path.name}")

    def toggle_same_art(self):
        if self.same_art_var.get() and self.art_paths[BMP_OFFSETS[0]]:
            path = self.art_paths[BMP_OFFSETS[0]]
            self.art_paths[BMP_OFFSETS[1]] = path
            self.art_images[BMP_OFFSETS[1]] = self.art_images[BMP_OFFSETS[0]].copy()
        self._refresh_previews()

    def _photo(self, image):
        _, ImageTk = require_pillow()
        thumb = image.copy()
        thumb.thumbnail((320, 182))
        return ImageTk.PhotoImage(thumb, master=self.root)

    def _refresh_previews(self):
        if not hasattr(self, "preview_widgets"):
            return
        self._photo_refs = []
        for offset in BMP_OFFSETS:
            current, new, choose = self.preview_widgets[offset]
            original = self.originals.get(offset)
            art = self.art_images.get(offset)
            if original:
                current_photo = self._photo(original)
                self._photo_refs.append(current_photo)
                current.configure(image=current_photo, text="")
            else:
                current.configure(image="", text="Load p5.img first")
            if art:
                new_photo = self._photo(art)
                self._photo_refs.append(new_photo)
                new.configure(image=new_photo, text="")
            else:
                new.configure(image="", text="Choose artwork")
            choose.configure(state="disabled" if offset == BMP_OFFSETS[1] and self.same_art_var.get() else "normal")

    def save_extracted_previews(self):
        if not self.originals:
            messagebox.showinfo("Load p5.img first", "Load a p5.img to extract the two current splash BMPs.", parent=self.root)
            return
        folder = filedialog.askdirectory(title="Choose a folder for the two PNG previews")
        if not folder:
            return
        try:
            for offset in BMP_OFFSETS:
                name = "startup-brief.png" if offset == BMP_OFFSETS[0] else "main-boot.png"
                self.originals[offset].save(Path(folder) / name)
            self._log(f"Saved both extracted previews to {folder}.")
            messagebox.showinfo("Previews saved", "Saved startup-brief.png and main-boot.png.", parent=self.root)
        except Exception as exc:
            messagebox.showerror("Could not save previews", str(exc), parent=self.root)

    def create_patched_image(self):
        if not self.source_path or not self.source_bytes:
            messagebox.showinfo("Load p5.img first", "Load and validate a p5.img before creating the patched image.", parent=self.root)
            return
        if any(self.art_images.get(offset) is None for offset in BMP_OFFSETS):
            messagebox.showinfo("Choose artwork", "Choose artwork for both splash positions, or enable the same-image option.", parent=self.root)
            return
        default = self.source_path.with_name("p5_patched.img")
        filename = filedialog.asksaveasfilename(
            title="Save patched p5 image",
            initialdir=str(default.parent),
            initialfile=default.name,
            defaultextension=".img",
            filetypes=[("p5 image", "*.img")],
        )
        if not filename:
            return
        destination = Path(filename)
        if destination.resolve() == self.source_path.resolve():
            messagebox.showerror("Choose another file", "The patched image must not overwrite the original p5.img.", parent=self.root)
            return
        try:
            data = bytearray(self.source_bytes)
            for offset in BMP_OFFSETS:
                patch_bmp(data, offset, self.art_images[offset])
            destination.write_bytes(data)
            digest = sha256_file(destination)
            self.patched_path = destination
            self.output_var.set(f"Ready: {destination}  ·  SHA-256 {digest}")
            self._log(f"Created patched image: {destination} ({len(data):,} bytes), SHA-256 {digest}")
            self.status_var.set("Patched image created. Continue to Install and verify when ready.")
            self.notebook.select(self.flash_tab)
        except Exception as exc:
            messagebox.showerror("Could not create patched image", str(exc), parent=self.root)

    def flash_image(self):
        if not self.patched_path or not self.patched_path.is_file():
            messagebox.showinfo("Create a patched image first", "Choose artwork and create p5_patched.img first.", parent=self.root)
            return
        try:
            address, username, password = self._connection()
        except Exception as exc:
            messagebox.showerror("Connection details", str(exc), parent=self.root)
            return
        local_image = self.patched_path
        expected_hash = sha256_file(local_image)
        if local_image.stat().st_size != P5_SIZE:
            messagebox.showerror("Wrong image size", f"Expected {P5_SIZE:,} bytes.", parent=self.root)
            return
        suffix = uuid.uuid4().hex[:10]
        remote_patch = f"/home/{username}/qidi-splash-{suffix}-patched.img"
        remote_backup = f"/home/{username}/qidi-splash-{suffix}-before-flash.img"
        remote_verify = f"/home/{username}/qidi-splash-{suffix}-verify.img"

        def job():
            client = self._connect(address, username, password)
            try:
                target_size = self._sudo(client, password, f"blockdev --getsize64 {REMOTE_PARTITION}").strip()
                if target_size != str(P5_SIZE):
                    raise RuntimeError(f"Target partition is {target_size} bytes; expected {P5_SIZE}. Nothing was flashed.")
                self._log("Uploading the patched image…")
                with client.open_sftp() as sftp:
                    sftp.put(str(local_image), remote_patch)
                    if sftp.stat(remote_patch).st_size != P5_SIZE:
                        raise RuntimeError("Uploaded file size does not match. Nothing was flashed.")
                uploaded_hash = self._sudo(client, password, f"sha256sum {shlex.quote(remote_patch)}").split()[0].lower()
                if uploaded_hash != expected_hash.lower():
                    raise RuntimeError("Upload hash differs from the local image. Nothing was flashed.")

                self._log("Making and checking a backup of the current p5 partition…")
                self._sudo(client, password, f"dd if={REMOTE_PARTITION} of={shlex.quote(remote_backup)} bs=1M conv=fsync")
                with client.open_sftp() as sftp:
                    if sftp.stat(remote_backup).st_size != P5_SIZE:
                        raise RuntimeError("Printer-side backup has the wrong size. Nothing was flashed.")
                    backup_folder = local_image.parent / "backups"
                    backup_folder.mkdir(exist_ok=True)
                    backup_path = backup_folder / f"p5_before_flash_{datetime.now():%Y%m%d_%H%M%S}.img"
                    sftp.get(remote_backup, str(backup_path))
                if backup_path.stat().st_size != P5_SIZE:
                    raise RuntimeError("Downloaded backup has the wrong size. Nothing was flashed.")
                remote_backup_hash = self._sudo(client, password, f"sha256sum {shlex.quote(remote_backup)}").split()[0].lower()
                local_backup_hash = sha256_file(backup_path).lower()
                if local_backup_hash != remote_backup_hash:
                    raise RuntimeError("Downloaded backup hash does not match the printer copy. Nothing was flashed.")
                self._log(f"Verified backup saved to {backup_path}.")

                self._log("Writing the new image to /dev/mmcblk0p5…")
                self._sudo(client, password, f"dd if={shlex.quote(remote_patch)} of={REMOTE_PARTITION} bs=1M conv=fsync")
                self._log("Reading the partition back for verification…")
                self._sudo(client, password, f"dd if={REMOTE_PARTITION} of={shlex.quote(remote_verify)} bs=1M conv=fsync")
                readback_hash = self._sudo(client, password, f"sha256sum {shlex.quote(remote_verify)}").split()[0].lower()
                if readback_hash != expected_hash.lower():
                    raise RuntimeError(
                        "Read-back hash does not match. The printer will not be restarted. "
                        f"The original backup is at {backup_path} and {remote_backup} on the printer."
                    )
                self._log(f"Flash verified. SHA-256: {readback_hash}")
                self._log(f"Keep the backup: {backup_path}")
            finally:
                client.close()

            self._reboot_and_wait(address, username, password)
            return {"hash": readback_hash, "backup": str(backup_path)}

        def done(result):
            self._log(f"Restart complete. Printer is reachable again. Backup: {result['backup']}")
            self.status_var.set("Printer is back online. Check the new splash screen.")

        self._start_job("Backing up, flashing, restarting, and reconnecting", job, done)

    def _reboot_and_wait(self, address: str, username: str, password: str):
        self._log("Sending restart request…")
        client = self._connect(address, username, password)
        try:
            stdin, _stdout, _stderr = client.exec_command("sudo -S -p '' reboot", timeout=15)
            if password:
                stdin.write(password + "\n")
                stdin.flush()
            stdin.channel.shutdown_write()
        finally:
            client.close()

        timeout_seconds = 180
        interval_seconds = 4
        started = time.monotonic()
        saw_disconnect = False
        self._log("Waiting for the printer to restart and SSH to return (maximum 3 minutes)…")
        while time.monotonic() - started < timeout_seconds:
            try:
                probe = self._connect(address, username, password)
                try:
                    _stdin, stdout, _stderr = probe.exec_command("true", timeout=8)
                    if stdout.channel.recv_exit_status() != 0:
                        raise RuntimeError("SSH probe command failed")
                finally:
                    probe.close()
                if saw_disconnect:
                    self._log("SSH reconnected after the printer restarted.")
                    return
            except Exception:
                if not saw_disconnect:
                    self._log("Printer is restarting; waiting for SSH to disconnect and return…")
                saw_disconnect = True
            time.sleep(interval_seconds)

        raise RuntimeError(
            "Restart was requested, but SSH did not confirm that the printer went offline and returned within 3 minutes. "
            "Check the printer manually; the splash flash was already verified."
        )


def main():
    root = tk.Tk()
    QidiSplashTool(root)
    root.mainloop()


if __name__ == "__main__":
    main()

