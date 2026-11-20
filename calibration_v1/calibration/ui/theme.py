from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtCore import Qt, QRectF

STYLE = '''
* { font-family: "Malgun Gothic"; font-size: 12px; color: #243630; }
QMainWindow, QDialog { background: #f3f6f5; }
QLabel { background: transparent; }
QLabel#Title { font-size: 23px; font-weight: 700; color: #142d24; }
QLabel#Subtitle { color: #75847e; font-size: 11px; }
QLabel#SectionTitle { font-size: 15px; font-weight: 700; }
QLabel#Badge { background: #e7eeeb; color: #49665b; border-radius: 10px; padding: 4px 9px; }
QFrame#Card { background: #ffffff; border: 1px solid #dfe7e3; border-radius: 12px; }
QPushButton { background: #ffffff; border: 1px solid #d6e0db; border-radius: 7px; padding: 8px 13px; }
QPushButton:hover { background: #edf5f1; border-color: #9bbaab; }
QPushButton:pressed { background: #dcece4; }
QPushButton:disabled { color: #9ca9a2; background: #f1f4f2; border-color: #e1e7e3; }
QPushButton#Primary { background: #197b5c; border-color: #197b5c; color: #ffffff; font-weight: 700; }
QPushButton#Primary:hover { background: #126448; }
QPushButton#Primary:disabled { background: #bad3c8; border-color: #bad3c8; color: #f9fcfa; }
QPushButton#Stop { color: #a63636; border-color: #e4bbbb; background: #fff7f7; font-weight: 700; }
QPushButton#Stop:hover { background: #fbe8e8; }
QPushButton#Tab { border: none; padding: 9px 18px; color: #77877e; background: transparent; }
QPushButton#Tab:checked { color: #197b5c; font-weight: 700; background: #e3eee8; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: #ffffff; border: 1px solid #d5e0da; border-radius: 6px; padding: 6px 8px; min-height: 19px; selection-background-color: #cde8da; selection-color: #123f2d; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #328d6b; }
QComboBox::drop-down { border: none; width: 22px; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 15px; height: 15px; }
QGroupBox { border: 1px solid #dbe4df; border-radius: 8px; margin-top: 13px; padding: 16px 12px 12px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 13px; padding: 0 5px; }
QTableView { background: #ffffff; border: none; gridline-color: #edf1ee; selection-background-color: #dbede3; selection-color: #153d2b; outline: 0; }
QTableView::item { padding: 8px 10px; border-bottom: 1px solid #edf1ee; }
QHeaderView::section { background: #f7faf8; color: #708279; padding: 10px 8px; border: none; border-bottom: 1px solid #dce5df; font-size: 11px; }
QHeaderView::section:hover { color: #197b5c; background: #edf5f0; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #c9d5ce; border-radius: 4px; min-height: 30px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #c9d5ce; border-radius: 4px; min-width: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QPlainTextEdit { background: #f8faf9; border: 1px solid #dde6e0; border-radius: 7px; padding: 8px; }
QProgressBar { background: #e4ece7; border: none; border-radius: 3px; height: 5px; text-align: center; }
QProgressBar::chunk { background: #299675; border-radius: 3px; }
QMenu { background: white; border: 1px solid #dbe5de; padding: 5px; }
QMenu::item { padding: 8px 24px; }
QMenu::item:selected { background: #e4f1e9; }
QToolTip { background: #173b2a; color: white; border: none; padding: 7px; }
'''

def app_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor('#197b5c'))
    painter.drawRoundedRect(QRectF(0, 0, 64, 64), 15, 15)
    painter.setPen(QPen(QColor('#ffffff'), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    from PySide6.QtGui import QPainterPath
    path = QPainterPath()
    path.moveTo(12, 34)
    path.lineTo(23, 34)
    path.lineTo(29, 18)
    path.lineTo(37, 46)
    path.lineTo(43, 30)
    path.lineTo(53, 30)
    painter.drawPath(path)
    painter.end()
    return QIcon(pixmap)
