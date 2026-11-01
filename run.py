"""``python run.py <command>`` from any directory: the same commands as
``python -m platforms`` (see platforms/__main__.py). Python puts a script's
own folder on its import path, so this finds the package wherever it is run."""

from platforms.__main__ import main

if __name__ == "__main__":
    main()
