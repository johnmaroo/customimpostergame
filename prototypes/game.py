import os
import random


def clear_terminal():
    # Clear screen for Windows
    if os.name == 'nt':
        _ = os.system('cls')
    # Clear screen for macOS and Linux
    else:
        _ = os.system('clear')

def send_imessage(recipient, message):
        cmd = f"""osascript -e 'tell application "Messages" to send "{message}" to buddy "{recipient}" of (service 1 whose service type is iMessage)'"""
        os.system(cmd)

def wordbank_generator():
    words = []
    done = ''
    while done != "No":
        word = input("Input a word for the wordbank")
        words.append(word)
        done = input("Would you like to add more words? (Yes/No)")
        clear_terminal()
    return words

def setup():
    num_players = input("How many people are you playing with?")
    phonenumbers = []
    num_imposters = input("How many imposters would you like?")
    for player in num_players:
        number = input("Input your phone number (no dashes or parentheses)")
        phonenumbers.append(number)
    wordbank = wordbank_generator()
    return [phonenumbers, wordbank, num_imposters]

def round(phonenumbers, wordbank, num_imposters):
    chosen = random.choice(wordbank)
    wordbank.remove(chosen)
    imposters = random.sample(phonenumbers, num_imposters)
    faithfuls = [p for p in phonenumbers if p not in imposters]
    for imposter in imposters:
        send_imessage(imposter, "Imposter!")
    for faithful in faithfuls:
        send_imessage(faithful, chosen)
    clear_terminal()

def main():
    phonenumbers, wordbank, num_imposters = setup()
    done = ''
    while done != "No":
        round(phonenumbers, wordbank, num_imposters)
        done = input("Would you like to play again? (Yes/No)")

        if done == "wordbank":
            print(wordbank)
    
if __name__ == "__main__":
    main()