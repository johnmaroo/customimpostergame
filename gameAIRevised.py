"""Imposter for macOS: persistent word bank plus AI category clues for imposters.

Set AI_GATEWAY_API_KEY in a local .env file (see .env.example) so category
clues can be generated through the Vercel AI Gateway.
"""

from __future__ import annotations

import os
import random
from collections.abc import Callable

from clues import get_or_create_clue, load_env_file, resolve_api_key
from wordbank import WordBank

load_env_file()


def clear_terminal() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _escape_for_applescript(s: str) -> str:
    """Escape quotes so the message is safe for AppleScript."""
    return s.replace('"', '\\"')


def send_imessage(recipient: str, message: str, service_hint: str | None = None) -> None:
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
        service_clause = f"of {service_hint}"
    else:
        service_clause = "of (service 1 whose service type is iMessage)"

    osa = (
        f"""osascript -e 'tell application "Messages" to send "{msg}" """
        f"""to buddy "{rcpt}" {service_clause}' """
    )
    os.system(osa)


def prompt_int(message: str) -> int:
    raw = input(message).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit("Please enter a whole number.") from exc


def prompt_yes_no(message: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    answer = input(f"{message} ({suffix}): ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def imposter_message(category_clue: str | None) -> str:
    if category_clue:
        return f"You are the Imposter.\nCategory: {category_clue}"
    return "You are the Imposter."


def add_word_to_session(
    bank: WordBank,
    session: list[str],
    word: str,
    *,
    generate_clue: Callable[[str], str] | None = None,
) -> bool:
    """Save `word` to the database and session list. Returns True if newly inserted."""
    added = bank.add_word(word)
    already = {item.casefold() for item in session}
    if word.casefold() not in already:
        session.append(" ".join(word.strip().split()))
    get_or_create_clue(bank, word, generate=generate_clue)
    return added


def collect_wordbank(
    bank: WordBank,
    *,
    generate_clue: Callable[[str], str] | None = None,
    input_fn: Callable[[str], str] = input,
) -> list[str]:
    session: list[str] = []
    saved = bank.all_words()
    if saved:
        print(f"\nFound {len(saved)} word(s) saved from previous games.")
        if prompt_yes_no("Use the saved word bank this game?", default=True):
            session.extend(saved)
            print(f"Loaded {len(saved)} saved word(s).")

    print("\nAdd words to the word bank (each one is saved). Leave blank to stop.")
    while True:
        word = input_fn("Word: ").strip()
        if not word:
            break
        add_word_to_session(bank, session, word, generate_clue=generate_clue)
        clear_terminal()

    if not session:
        raise SystemExit("No words in the word bank. Add at least one word to play.")

    for word in session:
        get_or_create_clue(bank, word, generate=generate_clue)
    return session


def setup(
    bank: WordBank,
    *,
    generate_clue: Callable[[str], str] | None = None,
) -> tuple[list[str], list[str], int]:
    num_players = prompt_int("How many people are you playing with? ")
    if num_players < 3:
        raise SystemExit("You need at least 3 players.")
    phonenumbers: list[str] = []
    print("\nEnter phone numbers (include +country code if needed):")
    for i in range(num_players):
        while True:
            number = input(f"Player {i + 1} phone number: ").strip()
            if not number:
                print("A phone number is required.")
                continue
            if number in phonenumbers:
                print("That number is already in the game. Each player needs their own.")
                continue
            phonenumbers.append(number)
            break

    num_imposters = prompt_int("\nHow many imposters would you like? ")
    if num_imposters < 1:
        raise SystemExit("You need at least one imposter.")
    if num_imposters >= num_players:
        raise SystemExit("Imposters must be fewer than the number of players.")

    wordbank = collect_wordbank(bank, generate_clue=generate_clue)
    return phonenumbers, wordbank, num_imposters


def play_round(
    phonenumbers: list[str],
    wordbank: list[str],
    num_imposters: int,
    bank: WordBank,
    *,
    service_hint: str | None = None,
    send: Callable[..., None] = send_imessage,
    generate_clue: Callable[[str], str] | None = None,
) -> str:
    chosen = random.choice(wordbank)
    wordbank.remove(chosen)
    bank.mark_used(chosen)

    imposters = random.sample(phonenumbers, num_imposters)
    faithfuls = [player for player in phonenumbers if player not in imposters]
    clue = get_or_create_clue(bank, chosen, generate=generate_clue)

    for imposter in imposters:
        send(imposter, imposter_message(clue), service_hint=service_hint)
    for faithful in faithfuls:
        send(faithful, chosen, service_hint=service_hint)
    clear_terminal()
    return chosen


def main() -> None:
    if not resolve_api_key():
        print("Note: AI_GATEWAY_API_KEY is not set. Words will still be saved;")
        print("imposters will not get a category clue until a key is configured.")
        print("Copy .env.example to .env and add a Vercel AI Gateway key.\n")

    with WordBank() as bank:
        phonenumbers, wordbank, num_imposters = setup(bank)

        service_hint = None
        # If needed, you can set explicitly:
        # service_hint = 'service "E:yourAppleID@icloud.com"'

        while True:
            play_round(
                phonenumbers,
                wordbank,
                num_imposters,
                bank,
                service_hint=service_hint,
            )

            if not wordbank:
                again = input("\nWord bank is empty. Add more words? (y/n): ").strip().lower()
                if again in ("y", "yes"):
                    print("\nAdd words (each one is saved). Leave blank to stop.")
                    while True:
                        word = input("Word: ").strip()
                        if not word:
                            break
                        add_word_to_session(bank, wordbank, word, generate_clue=None)
                        clear_terminal()
                    if not wordbank:
                        print("Game over — no more words.")
                        break
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
