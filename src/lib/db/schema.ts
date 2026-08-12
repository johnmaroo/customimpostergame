import { integer, sqliteTable, text } from "drizzle-orm/sqlite-core";

export const words = sqliteTable("words", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  word: text("word").notNull(),
  wordNormalized: text("word_normalized").notNull().unique(),
  categoryClue: text("category_clue"),
  createdAt: text("created_at").notNull(),
});

export const games = sqliteTable("games", {
  id: text("id").primaryKey(),
  code: text("code").notNull().unique(),
  hostPlayerId: text("host_player_id").notNull(),
  hostToken: text("host_token").notNull(),
  numImposters: integer("num_imposters").notNull(),
  status: text("status").notNull(),
  currentWord: text("current_word"),
  currentCategory: text("current_category"),
  createdAt: text("created_at").notNull(),
});

export const players = sqliteTable("players", {
  id: text("id").primaryKey(),
  gameId: text("game_id").notNull(),
  name: text("name").notNull(),
  token: text("token").notNull(),
  isImposter: integer("is_imposter", { mode: "boolean" }).notNull().default(false),
  createdAt: text("created_at").notNull(),
});

export const gameWords = sqliteTable("game_words", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  gameId: text("game_id").notNull(),
  wordId: integer("word_id").notNull(),
  used: integer("used", { mode: "boolean" }).notNull().default(false),
});
