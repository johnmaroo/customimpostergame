import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import Database from "better-sqlite3";
import { drizzle } from "drizzle-orm/better-sqlite3";
import * as schema from "./schema";

const DB_PATH = process.env.SQLITE_PATH ?? "data/imposter.db";

const CREATE_SQL = `
CREATE TABLE IF NOT EXISTS words (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  word TEXT NOT NULL,
  word_normalized TEXT NOT NULL UNIQUE,
  category_clue TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS games (
  id TEXT PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  host_player_id TEXT NOT NULL,
  host_token TEXT NOT NULL,
  num_imposters INTEGER NOT NULL,
  status TEXT NOT NULL,
  current_word TEXT,
  current_category TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS players (
  id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL,
  name TEXT NOT NULL,
  token TEXT NOT NULL,
  is_imposter INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS game_words (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id TEXT NOT NULL,
  word_id INTEGER NOT NULL,
  used INTEGER NOT NULL DEFAULT 0
);
`;

function createDb() {
  mkdirSync(dirname(DB_PATH), { recursive: true });
  const sqlite = new Database(DB_PATH);
  sqlite.pragma("journal_mode = WAL");
  sqlite.exec(CREATE_SQL);
  return drizzle(sqlite, { schema });
}

let db: ReturnType<typeof createDb> | null = null;

export function getDb() {
  if (!db) {
    db = createDb();
  }
  return db;
}
