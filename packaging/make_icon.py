"""Make packaging/app.ico for the Windows build (needs Pillow)."""
from pathlib import Path

from roblox_agent.app.main import _make_icon_image

img = _make_icon_image(256)
out = Path(__file__).with_name("app.ico")
img.save(out, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote", out)
