#!/usr/bin/env python3
"""Launch the games in the browser.

Phones on the same Wi-Fi can join with the room code. Two games are served:
Imposter at `/`, and a Jeopardy buzzer at `/buzzer` where you host with a
password and a room of iPads races for the first tap.

The original Mac iMessage prototype still lives in prototypes/, and
`python gameAIRevised.py` still texts roles if you want that path.
"""

from server import main

if __name__ == "__main__":
    main()
