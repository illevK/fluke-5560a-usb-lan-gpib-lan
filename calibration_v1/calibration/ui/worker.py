"""A single background task owns all instrument I/O at a time."""
from PySide6.QtCore import QThread, Signal

class Job(QThread):
    result = Signal(object)
    error = Signal(object)
    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function

    def run(self):
        try:
            self.result.emit(self.function())
        except Exception as error:
            self.error.emit(error)
