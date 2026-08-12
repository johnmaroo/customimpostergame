import { cookies } from "next/headers";

export const PLAYER_ID_COOKIE = "imposter_pid";
export const PLAYER_TOKEN_COOKIE = "imposter_tok";

export async function readSession() {
  const store = await cookies();
  return {
    playerId: store.get(PLAYER_ID_COOKIE)?.value ?? null,
    token: store.get(PLAYER_TOKEN_COOKIE)?.value ?? null,
  };
}

export async function writeSession(playerId: string, token: string) {
  const store = await cookies();
  const options = {
    httpOnly: true,
    sameSite: "lax" as const,
    path: "/",
    maxAge: 60 * 60 * 24 * 7,
  };
  store.set(PLAYER_ID_COOKIE, playerId, options);
  store.set(PLAYER_TOKEN_COOKIE, token, options);
}
