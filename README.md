# QIDI Q2 boot splash tools

Unofficial tools to inspect and replace the boot splash images on a QIDI Q2. The tested offsets and timing below were confirmed on one Q2; other firmware versions and printer models are not yet verified. This project is not affiliated with QIDI.

## What is in `p5.img`?

`p5.img` is one 9 MiB image of the printer's `/dev/mmcblk0p5` partition. It contains two embedded 480 × 272, uncompressed 24-bit BMP splash images. It is not a bundle of two `.img` files.

| Offset | Boot image | Observed duration on the tested Q2 |
|---|---|---|
| `0x647000` | First splash | About 1 second |
| `0x6A6C00` | Main splash | For the rest of boot |

The patching tools replace only the BMP pixel data. By default, they put the same artwork in both slots so the old image does not flash briefly before the main splash. You can also provide separate artwork for each slot.

## Windows wizard

The graphical wizard can connect to the printer over SSH, download the current partition, preview both embedded images, choose artwork, prepare a patched image, make backups, flash, and verify the written data. It asks before flashing and offers reboot only after the read-back hash matches.

Install Python 3.10 or newer. On Windows, make sure the Python installer includes **Tcl/Tk**. Then open PowerShell in this folder and run:

```powershell
py -m pip install -r requirements.txt
py qidi_splash_tool.py
```

Enter your printer's IP address, SSH user (usually `mks`), and password in the wizard. The password is held in memory only. When prompted about an SSH host key, compare its fingerprint through a trusted channel before accepting.

## Terminal workflow (Windows and Linux)

Install Python 3.10 or newer and the packages listed in `requirements.txt`. Linux users may also need their distribution's `python3-tk` package for the graphical wizard; the terminal tools do not need Tkinter.

Replace `<printer-ip>` below with the printer's address. First open an interactive SSH session so `sudo` can ask for its password, make a backup, then download it:

```sh
ssh mks@<printer-ip>
sudo dd if=/dev/mmcblk0p5 of=/home/mks/p5_original.img bs=1M conv=fsync
stat -c '%s' /home/mks/p5_original.img
exit
scp mks@<printer-ip>:/home/mks/p5_original.img ./p5.img
```

Check that `p5.img` is exactly 9,437,184 bytes. Extract the two embedded images as separate preview files:

```sh
python extract_rsce_bmps.py
```

The previews are written under `output/`. The source partition image remains a single file.

Use one new image for both boot moments:

```sh
python patch_bootlogo.py ./p5.img ./my-logo.png --output ./p5_patched.img
```

Or use separate images for the brief first splash and the main splash:

```sh
python patch_bootlogo.py ./p5.img --startup-artwork ./startup.png --main-artwork ./main.png --output ./p5_patched.img
```

The patcher center-crops and resizes artwork to 480 × 272. It checks both embedded BMP headers and writes a separate output image. Review the artwork before flashing.

Upload the output and start another interactive SSH session:

```sh
scp ./p5_patched.img mks@<printer-ip>:/home/mks/
ssh mks@<printer-ip>
```

On the printer, confirm the uploaded size and hash, and make a fresh backup before writing:

```sh
stat -c '%s' /home/mks/p5_patched.img
sha256sum /home/mks/p5_patched.img
sudo dd if=/dev/mmcblk0p5 of=/home/mks/p5_before_flash.img bs=1M conv=fsync
stat -c '%s' /home/mks/p5_before_flash.img
```

Compare the uploaded hash with the local file (`Get-FileHash .\p5_patched.img -Algorithm SHA256` in PowerShell or `sha256sum ./p5_patched.img` on Linux). Stop if the image or backup is not 9,437,184 bytes or the hashes differ. Copy the backup off the printer before continuing.

Only after confirming the correct printer, partition, image size, transfer hash, and backup, flash and read back the partition:

```sh
sudo dd if=/home/mks/p5_patched.img of=/dev/mmcblk0p5 bs=1M conv=fsync
sudo dd if=/dev/mmcblk0p5 of=/tmp/p5_verify.img bs=1M conv=fsync
sha256sum /home/mks/p5_patched.img /tmp/p5_verify.img
```

Reboot only if the two hashes match:

```sh
sudo reboot
```

Do not interrupt the write. If the read-back hash differs, do not reboot; investigate and restore the backup if needed.

## Example artwork

![Q2 boot splash artwork example](docs/images/example-splash.png)

*Example artwork included with permission from the project owner. It is cropped and resized to the panel's native 480 × 272 resolution before patching.*

## Included tools

- `qidi_splash_tool.py` — graphical Windows/Linux wizard.
- `extract_bmps.py` — finds BMP images embedded in a firmware image.
- `extract_rsce_bmps.py` — extracts BMPs from the QIDI `RSCE` resource section.
- `patch_bootlogo.py` — replaces the two splash images.

Firmware images, extracted BMP previews, generated outputs, backups, and other personal artwork are not included. Use a fresh partition image from your own printer. Do not assume the tested offsets or partition apply to another model or firmware version.

