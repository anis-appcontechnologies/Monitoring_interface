# AMC Monitoring Interface

A professional desktop monitoring and oscilloscope interface for AMC motor controllers, built with PySide6.

## Features

- Real-time waveform acquisition (single-shot, real-time, scroll modes)
- Multi-channel oscilloscope with ELF symbol variable binding
- Electrical parameter configuration panels
- Dark / light theme with per-component QSS
- Serial communication with AMC firmware over UART

## Requirements

```
PySide6
pyserial
matplotlib
numpy
pyelftools
```

Install with:

```bash
pip install PySide6 pyserial matplotlib numpy pyelftools
```

## Project Structure

```
AMC_Interface/
├── amc_interface_qt.py     # Main application entry point and tab host
├── scope_qt.py             # Oscilloscope / monitoring panel
├── electrical_params_qt.py # Electrical parameter panel
├── inertia_param_qt.py     # Inertia parameter panel
├── load_params_qt.py       # Load parameter panel
├── save_params_qt.py       # Parameter save/load panel
├── terminal_qt.py          # Serial terminal panel
├── si_format.py            # SI unit formatter utility
├── assets/                 # Icons, logos, images
├── docs/                   # Protocol documentation, datasheets
├── scripts/                # Build, packaging, utility scripts
└── tests/                  # Unit and integration tests
```

## Running

```bash
python amc_interface_qt.py
```

## Communication Protocol

See `docs/AMC_Comm_Protocol_Doc.md` for the full serial protocol specification.

## License

Copyright © 2025–2026 Appcon Technologies. All rights reserved.
