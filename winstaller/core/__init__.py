"""Winstaller core: catalog, winget, system actions, task runner and updater.

This package must never import Qt/PySide/tkinter. Everything here is synchronous;
the UI layer is responsible for running it on a worker thread.
"""
