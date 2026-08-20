from __future__ import annotations

import os

import pytest


# The test suite exercises Qt value and widget helpers without opening the
# desktop application. Use Qt's headless platform so the same tests work on
# hosted runners that do not have an interactive display session.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets


@pytest.fixture(scope="session", autouse=True)
def qt_application():
    """Provide one headless QApplication for all Qt-dependent tests."""
    application = QtWidgets.QApplication.instance()
    if application is None:
        application = QtWidgets.QApplication([])
    yield application
