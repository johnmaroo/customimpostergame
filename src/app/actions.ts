"use server";

import { randomUUID } from "node:crypto";
import { and, eq } from "drizzle-orm";
import { redirect } from "next/navigation";
import { generateCategoryClue } from "@/lib/clues";
import { getDb } from "@/lib/db";
import { games, gameWords, players, words } from "@/lib/db/schema";
import { dealRound, makeRoomCode, normalizeWord, playerView } from "@/lib/game";
import { readSession, writeSession } from "@/lib/session";

export type GameState = {
  code: string;
  isHost: boolean;
  status: string;
  playerNames: string[];
  unusedWordCount: number;
  savedWordCount: number;
  numImposters: number;
  you: { id: string; name: string } | null;
  view: ReturnType<typeof playerView> | null;
};

function nowIso() {
  return new Date().toISOString();
}

async function requirePlayer(code: string) {
  const db = getDb();
  const session = await readSession();
  const game = db.select().from(games).where(eq(games.code, code.toUpperCase())).get();
  if (!game) {
    throw new Error("Game not found.");
  }
  if (!session.playerId || !session.token) {
    throw new Error("Join this game from your phone first.");
  }
  const player = db
    .select()
    .from(players)
    .where(and(eq(players.id, session.playerId), eq(players.gameId, game.id)))
    .get();
  if (!player || player.token !== session.token) {
    throw new Error("Join this game from your phone first.");
  }
  return { db, game, player };
}

export async function createGame(formData: FormData) {
  const name = String(formData.get("name") ?? "").trim();
  const numImposters = Number(formData.get("imposters") ?? 1);
  if (!name) {
    throw new Error("Enter your name.");
  }
  if (!Number.isInteger(numImposters) || numImposters < 1) {
    throw new Error("You need at least one imposter.");
  }

  const db = getDb();
  const playerId = randomUUID();
  const token = randomUUID();
  let code = makeRoomCode();
  while (db.select().from(games).where(eq(games.code, code)).get()) {
    code = makeRoomCode();
  }
  const gameId = randomUUID();
  const createdAt = nowIso();

  db.insert(games)
    .values({
      id: gameId,
      code,
      hostPlayerId: playerId,
      hostToken: token,
      numImposters,
      status: "lobby",
      createdAt,
    })
    .run();
  db.insert(players)
    .values({
      id: playerId,
      gameId,
      name,
      token,
      isImposter: false,
      createdAt,
    })
    .run();

  await writeSession(playerId, token);
  redirect(`/g/${code}`);
}

export async function joinGame(formData: FormData) {
  const name = String(formData.get("name") ?? "").trim();
  const code = String(formData.get("code") ?? "").trim().toUpperCase();
  if (!name) {
    throw new Error("Enter your name.");
  }
  if (!code) {
    throw new Error("Enter a room code.");
  }

  const db = getDb();
  const game = db.select().from(games).where(eq(games.code, code)).get();
  if (!game) {
    throw new Error("Game not found.");
  }

  const playerId = randomUUID();
  const token = randomUUID();
  db.insert(players)
    .values({
      id: playerId,
      gameId: game.id,
      name,
      token,
      isImposter: false,
      createdAt: nowIso(),
    })
    .run();

  await writeSession(playerId, token);
  redirect(`/g/${code}`);
}

export async function addWord(code: string, word: string) {
  const cleaned = word.trim().replace(/\s+/g, " ");
  if (!cleaned) {
    throw new Error("Enter a word.");
  }
  const { db, game, player } = await requirePlayer(code);
  if (player.id !== game.hostPlayerId) {
    throw new Error("Only the host can add words.");
  }

  const normalized = normalizeWord(cleaned);
  let row = db.select().from(words).where(eq(words.wordNormalized, normalized)).get();
  if (!row) {
    const clue = await generateCategoryClue(cleaned);
    db.insert(words)
      .values({
        word: cleaned,
        wordNormalized: normalized,
        categoryClue: clue,
        createdAt: nowIso(),
      })
      .run();
    row = db.select().from(words).where(eq(words.wordNormalized, normalized)).get();
  } else if (!row.categoryClue) {
    const clue = await generateCategoryClue(row.word);
    if (clue) {
      db.update(words).set({ categoryClue: clue }).where(eq(words.id, row.id)).run();
      row = { ...row, categoryClue: clue };
    }
  }
  if (!row) {
    throw new Error("Could not save that word.");
  }

  const already = db
    .select()
    .from(gameWords)
    .where(and(eq(gameWords.gameId, game.id), eq(gameWords.wordId, row.id)))
    .get();
  if (!already) {
    db.insert(gameWords)
      .values({ gameId: game.id, wordId: row.id, used: false })
      .run();
  }
}

export async function addSavedWords(code: string) {
  const { db, game, player } = await requirePlayer(code);
  if (player.id !== game.hostPlayerId) {
    throw new Error("Only the host can add words.");
  }
  const saved = db.select().from(words).all();
  for (const row of saved) {
    const already = db
      .select()
      .from(gameWords)
      .where(and(eq(gameWords.gameId, game.id), eq(gameWords.wordId, row.id)))
      .get();
    if (!already) {
      db.insert(gameWords)
        .values({ gameId: game.id, wordId: row.id, used: false })
        .run();
    }
  }
}

export async function startRound(code: string) {
  const { db, game, player } = await requirePlayer(code);
  if (player.id !== game.hostPlayerId) {
    throw new Error("Only the host can start a round.");
  }

  const seated = db.select().from(players).where(eq(players.gameId, game.id)).all();
  const unused = db
    .select({
      id: gameWords.id,
      word: words.word,
      categoryClue: words.categoryClue,
    })
    .from(gameWords)
    .innerJoin(words, eq(gameWords.wordId, words.id))
    .where(and(eq(gameWords.gameId, game.id), eq(gameWords.used, false)))
    .all();

  const dealt = dealRound(
    seated.map((item) => item.id),
    unused.map((item) => item.word),
    game.numImposters,
  );
  const chosen = unused.find((item) => item.word === dealt.word);
  if (!chosen) {
    throw new Error("Word bank is empty. Add more words.");
  }

  db.update(gameWords).set({ used: true }).where(eq(gameWords.id, chosen.id)).run();
  db.update(players).set({ isImposter: false }).where(eq(players.gameId, game.id)).run();
  for (const imposterId of dealt.imposters) {
    db.update(players).set({ isImposter: true }).where(eq(players.id, imposterId)).run();
  }
  db.update(games)
    .set({
      status: "round",
      currentWord: dealt.word,
      currentCategory: chosen.categoryClue,
    })
    .where(eq(games.id, game.id))
    .run();
}

export async function getGameState(code: string): Promise<GameState> {
  const db = getDb();
  const game = db.select().from(games).where(eq(games.code, code.toUpperCase())).get();
  if (!game) {
    throw new Error("Game not found.");
  }
  const session = await readSession();
  const seated = db.select().from(players).where(eq(players.gameId, game.id)).all();
  const player = seated.find((item) => item.id === session.playerId && item.token === session.token) ?? null;
  const unusedWordCount = db
    .select()
    .from(gameWords)
    .where(and(eq(gameWords.gameId, game.id), eq(gameWords.used, false)))
    .all().length;
  const savedWordCount = db.select().from(words).all().length;
  const isHost = Boolean(player && player.id === game.hostPlayerId);

  let view: GameState["view"] = null;
  if (player && game.status === "round" && game.currentWord) {
    const imposters = seated.filter((item) => item.isImposter).map((item) => item.id);
    view = playerView(player.id, game.currentWord, imposters, game.currentCategory);
  }

  return {
    code: game.code,
    isHost,
    status: game.status,
    playerNames: seated.map((item) => item.name),
    unusedWordCount,
    savedWordCount,
    numImposters: game.numImposters,
    you: player ? { id: player.id, name: player.name } : null,
    view,
  };
}
