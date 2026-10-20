#!/bin/bash
# Installs the lip-sync engine (MuseTalk v1.5, Apple Silicon port) into tools/.musetalk with its own Python environment, about 4 GB of weights and about 3 GB of packages.
# Licenses: MuseTalk code and model MIT, sd-vae-ft-mse MIT, whisper-tiny Apache-2.0. Everything stays on this Mac. Needs Apple Silicon, Python 3.11 and about 8 GB of free disk.
#   bash tools/setup_lipsync.sh
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON311:-$(ls -d "$HOME"/.local/share/uv/python/cpython-3.11*/bin/python3.11 2>/dev/null | head -1)}
[ -x "${PY:-}" ] || PY=$(command -v python3.11 || true)
[ -x "${PY:-}" ] || { echo "Python 3.11 is needed (for example: uv python install 3.11)"; exit 1; }
[ -d .musetalk ] || git clone --depth 1 https://github.com/barnent1/musetalk-mac.git .musetalk
cd .musetalk
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install -r requirements-mac.txt
PATH="$PWD/.venv/bin:$PATH" bash ./download_weights_mac.sh
# The port's .gitignore ("models/") swallowed two source files and a folder its server needs: take the two files from the official MuseTalk repo (MIT) and add Apple Silicon (mps) device choice.
mkdir -p upstream/musetalk/models upstream/data/demo_five
for f in vae.py unet.py; do curl -fsSL "https://raw.githubusercontent.com/TMElyralab/MuseTalk/main/musetalk/models/$f" -o "upstream/musetalk/models/$f"; done
touch upstream/musetalk/models/__init__.py
sed -i.bak 's/self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")/self.device = torch.device("cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu")/' upstream/musetalk/models/vae.py upstream/musetalk/models/unet.py
sed -i.bak 's/weights = torch.load(model_path) if torch.cuda.is_available() else torch.load(model_path, map_location=self.device)/weights = torch.load(model_path, map_location="cpu" if not torch.cuda.is_available() else None)/' upstream/musetalk/models/unet.py
# batch size from the environment (the studio sets MUSETALK_BATCH=32 and MUSETALK_FP16_UNET=1: about 20% faster, same picture: PSNR 51 dB, SSIM 0.9975)
sed -i.bak 's/def run_lipsync(avatar: dict, audio_bytes: bytes, batch_size: int = 16) -> bytes:/def run_lipsync(avatar: dict, audio_bytes: bytes, batch_size: int = int(os.environ.get("MUSETALK_BATCH", "16"))) -> bytes:/' server.py
rm -f upstream/musetalk/models/*.bak server.py.bak
echo "Lip-sync engine ready. Open the CRM, tab Voice & Lip Sync."
