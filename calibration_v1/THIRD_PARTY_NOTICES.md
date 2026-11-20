# Third-party software

This application uses Python, PySide6 Essentials / Qt, Shiboken6, PyVISA, pyserial,
and the PyInstaller bootloader. Their licenses and source are available at:

- Python: https://docs.python.org/3/license.html
- Qt / PySide6 / Shiboken6: https://www.qt.io/licensing/ and https://code.qt.io/pyside/pyside-setup.git/
- Qt sources: https://code.qt.io/qt/qtbase.git/
- PyVISA (MIT): https://github.com/pyvisa/pyvisa/blob/main/LICENSE
- pyserial (BSD): https://github.com/pyserial/pyserial/blob/master/LICENSE.txt
- PyInstaller (GPL with bootloader exception): https://pyinstaller.org/en/stable/license.html

PySide6/Qt libraries are dynamically loaded, under LGPLv3 as applicable.
Application source and build instructions are in this project's `calibration_v1` directory.
The executable can be rebuilt against compatible replacement versions of the Qt libraries.
It contains no Qt WebEngine or browser runtime.
