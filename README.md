# Imposter

A phone party game. One person hosts a room, everyone else joins on their own phone, and each screen shows only that player's role.

This is the same game as the original Mac iMessage prototype in `prototypes/`, revised so nobody needs a computer or Messages.app.

## How to play

1. Open the site on your phone.
2. One person taps **Create room** and reads the 4-letter code out loud.
3. Everyone else taps **Join** and enters the code.
4. The host types secret words. Each word is saved to the database. The word disappears after save so the group cannot read it off the host's screen.
5. The host taps **Start round**. Each player taps **Reveal my role** on their own phone.
   - Most people see the secret word.
   - Imposters see **You are the Imposter** plus an AI category clue when a key is configured.
6. Talk, vote, then the host starts the next round.

## Run it

```bash
npm install
npm run dev
```

On your phone, open the computer's local URL (same Wi-Fi), or deploy the app and share that link.

To give imposters category clues, copy `.env.example` to `.env.local` and add an [AI Gateway API key](https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai-gateway%2Fapi-keys).
