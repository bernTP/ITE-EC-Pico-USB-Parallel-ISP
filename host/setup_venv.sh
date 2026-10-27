#!/usr/bin/env bash
# Create host/.venv with what ite_pp_isp.py needs.
# Run it on the machine the Pico is plugged into (e.g. the Raspberry Pi Zero):
# a venv is tied to the machine and Python that created it.
set -euo pipefail

cd "$(dirname "$0")"

if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
    echo "python3-venv is missing: sudo apt install python3-venv" >&2
    exit 1
fi
if command -v ldconfig >/dev/null && ! ldconfig -p 2>/dev/null | grep -q 'libusb-1.0'; then
    echo "warning: libusb-1.0 not found, pyusb needs it: sudo apt install libusb-1.0-0" >&2
fi

python3 -m venv .venv
if ls wheels/*.whl >/dev/null 2>&1; then
    # offline install from wheels shipped next to this script
    .venv/bin/pip install --no-index --find-links wheels -r requirements.txt
else
    .venv/bin/pip install -r requirements.txt
fi

cat <<EOF

Done. Run the tool with:
    $(pwd)/.venv/bin/python $(pwd)/ite_pp_isp.py hello

USB access without sudo (once):
    sudo cp $(pwd)/99-ite-pp-isp.rules /etc/udev/rules.d/
    sudo udevadm control --reload-rules && sudo udevadm trigger
EOF
