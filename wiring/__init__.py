"""Wiring Platform A and Platform B into the Operation Workbench.

Run with the *Workbench's* interpreter, from this folder:

    C:/Users/itsme/git/operation-workbench/.venv/Scripts/python -m wiring discover
    C:/Users/itsme/git/operation-workbench/.venv/Scripts/python -m wiring apply [--dry-run]
    C:/Users/itsme/git/operation-workbench/.venv/Scripts/python -m wiring check

It opens the Workbench in process and calls the same service methods the
Workbench's own API routes call, writing the same audit entries, under the
workspace's first administrator. Nothing here changes the Workbench's code.
"""
