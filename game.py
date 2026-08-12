#!/usr/bin/env python3
"""Launch the Imposter party game in the browser.

Phones on the same Wi-Fi can join with the room code. The original Mac
iMessage prototype still lives in prototypes/, and `python gameAIRevised.py`
still texts roles if you want that path.
"""

from server import main

if __name__ == "__main__":
    main()
