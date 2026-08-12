export type PlayerView =
  | { role: "imposter"; category: string | null }
  | { role: "faithful"; word: string };

export function sample<T>(items: T[], count: number): T[] {
  if (count < 0 || count > items.length) {
    throw new Error("cannot sample that many items");
  }
  const copy = [...items];
  for (let i = copy.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [copy[i], copy[j]] = [copy[j], copy[i]];
  }
  return copy.slice(0, count);
}

export function dealRound(
  playerIds: string[],
  unusedWords: string[],
  numImposters: number,
): { word: string; imposters: string[]; remainingWords: string[] } {
  if (unusedWords.length === 0) {
    throw new Error("Word bank is empty. Add more words.");
  }
  if (playerIds.length < 2) {
    throw new Error("Need at least two players.");
  }
  if (numImposters < 1) {
    throw new Error("You need at least one imposter.");
  }
  if (numImposters >= playerIds.length) {
    throw new Error("Imposters must be fewer than the number of players.");
  }

  const wordIndex = Math.floor(Math.random() * unusedWords.length);
  const word = unusedWords[wordIndex];
  const remainingWords = unusedWords.filter((_, index) => index !== wordIndex);
  const imposters = sample(playerIds, numImposters);
  return { word, imposters, remainingWords };
}

export function playerView(
  playerId: string,
  word: string,
  imposters: string[],
  category: string | null,
): PlayerView {
  if (imposters.includes(playerId)) {
    return { role: "imposter", category };
  }
  return { role: "faithful", word };
}

export function normalizeWord(word: string): string {
  return word.trim().replace(/\s+/g, " ").toLocaleLowerCase();
}

export function makeRoomCode(): string {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  let code = "";
  for (let i = 0; i < 4; i += 1) {
    code += alphabet[Math.floor(Math.random() * alphabet.length)];
  }
  return code;
}
