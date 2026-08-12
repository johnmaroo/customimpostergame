import { generateText, Output } from "ai";
import { z } from "zod";

const clueSchema = z.object({
  category: z.string(),
});

export async function generateCategoryClue(word: string): Promise<string | null> {
  const cleaned = word.trim();
  if (!cleaned) {
    return null;
  }
  if (!process.env.AI_GATEWAY_API_KEY && !process.env.VERCEL_OIDC_TOKEN) {
    return null;
  }

  try {
    const { output } = await generateText({
      model: process.env.IMPOSTER_CLUE_MODEL ?? "openai/gpt-5.4-mini",
      output: Output.object({ schema: clueSchema }),
      system:
        "You write category clues for the party game Imposter. " +
        "Reply with a short category the secret word belongs to, " +
        "so an imposter can talk around the topic without knowing the word. " +
        "Do not include the secret word itself.",
      prompt: `Secret word: ${cleaned}`,
    });
    const category = output?.category?.trim() ?? "";
    if (!category || category.toLocaleLowerCase().includes(cleaned.toLocaleLowerCase())) {
      return null;
    }
    return category;
  } catch {
    return null;
  }
}
