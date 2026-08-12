"use client";

import { useEffect, useState, useTransition } from "react";
import { addSavedWords, addWord, getGameState, startRound, type GameState } from "@/app/actions";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";

export function GameClient({ code }: { code: string }) {
  const [state, setState] = useState<GameState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [word, setWord] = useState("");
  const [revealedKey, setRevealedKey] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const roundKey =
    state?.status === "round" && state.view
      ? state.view.role === "faithful"
        ? `faithful:${state.view.word}`
        : `imposter:${state.view.category ?? ""}`
      : "lobby";
  const revealed = revealedKey === roundKey;

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const next = await getGameState(code);
        if (!cancelled) {
          setState(next);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Could not load this game.");
        }
      }
    }
    void tick();
    const id = window.setInterval(() => void tick(), 1500);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [code]);

  if (error && !state) {
    return (
      <main className="mx-auto flex min-h-full w-full max-w-md flex-col gap-4 px-4 py-10">
        <h1 className="text-2xl font-semibold">Imposter</h1>
        <p className="text-muted-foreground">{error}</p>
      </main>
    );
  }

  if (!state) {
    return (
      <main className="mx-auto flex min-h-full w-full max-w-md items-center justify-center px-4 py-10">
        <p className="text-muted-foreground">Opening the room…</p>
      </main>
    );
  }

  return (
    <main className="mx-auto flex min-h-full w-full max-w-md flex-col gap-6 px-4 py-8">
      <header className="flex items-start justify-between gap-4">
        <div>
          <p className="text-sm text-muted-foreground">Room</p>
          <p className="text-4xl font-semibold tracking-[0.25em]">{state.code}</p>
        </div>
        {state.isHost ? <Badge>Host</Badge> : <Badge variant="secondary">Player</Badge>}
      </header>

      {state.status === "round" && state.view ? (
        <RoleCard view={state.view} revealed={revealed} onReveal={() => setRevealedKey(roundKey)} />
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>Waiting for a round</CardTitle>
            <CardDescription>
              Keep your phone to yourself. The host will start when everyone is in.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Players</CardTitle>
          <CardDescription>
            {state.playerNames.length} in the room · {state.numImposters} imposter
            {state.numImposters === 1 ? "" : "s"}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          {state.playerNames.map((name, index) => (
            <Badge key={`${name}-${index}`} variant="secondary">
              {name}
            </Badge>
          ))}
        </CardContent>
      </Card>

      {state.isHost ? (
        <HostControls
          state={state}
          word={word}
          setWord={setWord}
          pending={pending}
          onAddWord={() => {
            const nextWord = word;
            setWord("");
            startTransition(async () => {
              try {
                await addWord(state.code, nextWord);
                setState(await getGameState(state.code));
                setError(null);
              } catch (err) {
                setError(err instanceof Error ? err.message : "Could not save that word.");
              }
            });
          }}
          onAddSaved={() => {
            startTransition(async () => {
              try {
                await addSavedWords(state.code);
                setState(await getGameState(state.code));
                setError(null);
              } catch (err) {
                setError(err instanceof Error ? err.message : "Could not load saved words.");
              }
            });
          }}
          onStart={() => {
            startTransition(async () => {
              try {
                await startRound(state.code);
                setRevealedKey(null);
                setState(await getGameState(state.code));
                setError(null);
              } catch (err) {
                setError(err instanceof Error ? err.message : "Could not start the round.");
              }
            });
          }}
        />
      ) : null}

      {error ? <p className="text-destructive text-sm">{error}</p> : null}
    </main>
  );
}

function RoleCard({
  view,
  revealed,
  onReveal,
}: {
  view: NonNullable<GameState["view"]>;
  revealed: boolean;
  onReveal: () => void;
}) {
  if (!revealed) {
    return (
      <Card className="min-h-48">
        <CardHeader>
          <CardTitle>Your phone only</CardTitle>
          <CardDescription>Tap when nobody else can see your screen.</CardDescription>
        </CardHeader>
        <CardContent>
          <Button className="h-14 w-full text-base" onClick={onReveal}>
            Reveal my role
          </Button>
        </CardContent>
      </Card>
    );
  }

  if (view.role === "imposter") {
    return (
      <Card className="border-destructive/40 min-h-48">
        <CardHeader>
          <CardTitle>You are the Imposter</CardTitle>
          <CardDescription>
            {view.category
              ? "Blend in using this category. Do not say the exact word — you do not have it."
              : "You do not get the secret word. Listen, bluff, and do not get caught."}
          </CardDescription>
        </CardHeader>
        {view.category ? (
          <CardContent>
            <p className="text-muted-foreground text-sm">Category</p>
            <p className="text-3xl font-semibold text-pretty">{view.category}</p>
          </CardContent>
        ) : null}
      </Card>
    );
  }

  return (
    <Card className="min-h-48">
      <CardHeader>
        <CardTitle>Your word</CardTitle>
        <CardDescription>Talk about this. Find the imposter.</CardDescription>
      </CardHeader>
      <CardContent>
        <p className="text-4xl font-semibold text-pretty">{view.word}</p>
      </CardContent>
    </Card>
  );
}

function HostControls({
  state,
  word,
  setWord,
  pending,
  onAddWord,
  onAddSaved,
  onStart,
}: {
  state: GameState;
  word: string;
  setWord: (value: string) => void;
  pending: boolean;
  onAddWord: () => void;
  onAddSaved: () => void;
  onStart: () => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Host</CardTitle>
        <CardDescription>
          Add words privately. They are saved for later games. The list stays hidden on this
          screen.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            onAddWord();
          }}
        >
          <Input
            value={word}
            onChange={(event) => setWord(event.target.value)}
            placeholder="Secret word"
            autoCapitalize="none"
            className="h-12"
          />
          <Button type="submit" variant="secondary" className="h-12 w-full" disabled={pending}>
            Save word
          </Button>
        </form>
        <p className="text-muted-foreground text-sm">
          {state.unusedWordCount} unused this game · {state.savedWordCount} saved in the database
        </p>
        {state.savedWordCount > 0 ? (
          <Button
            type="button"
            variant="outline"
            className="h-12 w-full"
            disabled={pending}
            onClick={onAddSaved}
          >
            Use saved words
          </Button>
        ) : null}
        <Separator />
        <Button type="button" className="h-14 w-full text-base" disabled={pending} onClick={onStart}>
          {state.status === "round" ? "Next round" : "Start round"}
        </Button>
      </CardContent>
    </Card>
  );
}
