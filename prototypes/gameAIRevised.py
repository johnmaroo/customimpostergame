"""This version was edited using AI based on the rudimentary game.py file"""

import os
import random

def clear_terminal():
    os.system("clear")

def _escape_for_applescript(s: str) -> str:
    """Escape quotes so the message is safe for AppleScript."""
    return s.replace('"', '\\"')

def send_imessage(recipient: str, message: str, service_hint: None):
    """
    Send an iMessage on macOS via AppleScript.
    recipient: phone number or Apple ID linked to iMessage
    message:   text to send
    service_hint: optionally specify the service explicitly
                  e.g. 'service "E:yourAppleID@icloud.com"'
    """
    msg = _escape_for_applescript(message)
    rcpt = _escape_for_applescript(recipient)

    if service_hint:
        service_clause = f'of {service_hint}'
    else:
        service_clause = 'of (service 1 whose service type is iMessage)'

    osa = f'''osascript -e 'tell application "Messages" to send "{msg}" to buddy "{rcpt}" {service_clause}' '''
    os.system(osa)

def wordbank_generator():
    words = []
    while True:
        word = input("Input a word for the word bank (blank to stop): ").strip()
        clear_terminal()
        if not word:
            break
        words.append(word)
    return words

def setup():
    num_players = int(input("How many people are you playing with? ").strip())
    phonenumbers = []
    print("\nEnter phone numbers (include +country code if needed):")
    for i in range(num_players):
        number = input(f"Player {i+1} phone number: ").strip()
        phonenumbers.append(number)

    num_imposters = int(input("\nHow many imposters would you like? ").strip())

    print("\nNow let's build the word bank.")
    wordbank = wordbank_generator()

    return phonenumbers, wordbank, num_imposters

def play_round(phonenumbers, wordbank, num_imposters, service_hint=None):
    # Pick a word for faithfuls
    chosen = random.choice(wordbank)
    wordbank.remove(chosen)

    # Pick imposters
    imposters = random.sample(phonenumbers, num_imposters)
    faithfuls = [p for p in phonenumbers if p not in imposters]

    # Notify players
    for imposter in imposters:
        send_imessage(imposter, "Imposter!", service_hint=service_hint)
    for faithful in faithfuls:
        send_imessage(faithful, chosen, service_hint=service_hint)
    clear_terminal()

def main():
    phonenumbers, wordbank, num_imposters = setup()

    service_hint = None
    # If needed, you can set explicitly:
    # service_hint = 'service "E:yourAppleID@icloud.com"'

    while True:
        play_round(phonenumbers, wordbank, num_imposters, service_hint=service_hint)

        if not wordbank:
            again = input("\nWord bank is empty. Add more words? (y/n): ").strip().lower()
            if again in ("y", "yes"):
                wordbank.extend(wordbank_generator())
            else:
                print("Game over — no more words.")
                break

        done = input("\nPlay another round? (y/n): ").strip().lower()
        if done == "phonenumbers":
            print(phonenumbers)
        elif done == "wordbank":
            print(wordbank)
        elif done not in ("y", "yes"):
            break

if __name__ == "__main__":
    main()
