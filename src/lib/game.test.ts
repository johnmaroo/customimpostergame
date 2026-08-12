import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { dealRound, normalizeWord, playerView } from "./game";

describe("dealRound", () => {
  it("picks one word, removes it, and chooses imposters", () => {
    const result = dealRound(["a", "b", "c"], ["toaster", "violin"], 1);
    assert.equal(["toaster", "violin"].includes(result.word), true);
    assert.equal(result.remainingWords.includes(result.word), false);
    assert.equal(result.remainingWords.length, 1);
    assert.equal(result.imposters.length, 1);
    assert.equal(["a", "b", "c"].includes(result.imposters[0]), true);
  });

  it("rejects an empty word bank", () => {
    assert.throws(() => dealRound(["a", "b"], [], 1), /Word bank is empty/);
  });

  it("rejects too many imposters", () => {
    assert.throws(() => dealRound(["a", "b"], ["x"], 2), /fewer than the number of players/);
  });
});

describe("playerView", () => {
  it("gives imposters a category and faithfuls the word", () => {
    assert.deepEqual(playerView("imp", "toaster", ["imp"], "kitchen appliances"), {
      role: "imposter",
      category: "kitchen appliances",
    });
    assert.deepEqual(playerView("p2", "toaster", ["imp"], "kitchen appliances"), {
      role: "faithful",
      word: "toaster",
    });
  });
});

describe("normalizeWord", () => {
  it("collapses case and spacing", () => {
    assert.equal(normalizeWord("  Apple Pie  "), "apple pie");
  });
});
