#!/usr/bin/env bash
set -euo pipefail

sudo apt update
sudo apt install -y python3-venv tesseract-ocr

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt

echo
echo "Running fast validation..."
PYTHONPATH=. python tests/test_v14_semantic.py
PYTHONPATH=. python tests/test_v15_addon.py

echo
echo "Setup complete. Start Streamlit with:"
echo "  source .venv/bin/activate"
echo "  streamlit run app.py"
