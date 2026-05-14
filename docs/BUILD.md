# AMC Interface — Build & Release Guide

## Development setup

```bash
pip install -r requirements.txt
```

## Run from source

```bash
python amc_interface_qt.py
```

## Run tests

```bash
python -m pytest tests/ -v
# or without pytest installed:
python tests/test_protocol.py
```

## Build standalone Windows executable

1. Install PyInstaller (one-time):
   ```bash
   pip install pyinstaller
   ```

2. Build:
   ```bash
   pyinstaller amc_interface.spec
   ```

3. Output location:
   ```
   dist/AMC_Interface/AMC_Interface.exe
   ```

4. Distribute:
   Zip the entire `dist/AMC_Interface/` folder and send to field engineers.
   The engineer only needs to unzip and double-click `AMC_Interface.exe`.
   No Python installation required on the target machine.

## Expected build size

~80–120 MB for the `dist/AMC_Interface/` folder (PySide6 + matplotlib bundled).

## Notes

- Icons require the `assets/` folder to be present when running from source.
- The `samples/` folder is not included in the build — it is for development only.
- CI runs automatically on every push to `main` via `.github/workflows/ci.yml`.
