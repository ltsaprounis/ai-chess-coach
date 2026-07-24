// The last player the user looked at, kept in localStorage so the header
// switcher and the "/" landing default to them. Pure + injectable,
// mirroring coachAgent.ts.

const STORAGE_KEY = "currentPlayer";

type PlayerStore = Pick<Storage, "getItem" | "setItem">;

/** The stored player, or null when unset or storage is unavailable. */
export function getStoredPlayer(
  store: PlayerStore = localStorage,
): string | null {
  try {
    return store.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setStoredPlayer(
  username: string,
  store: PlayerStore = localStorage,
): void {
  try {
    store.setItem(STORAGE_KEY, username);
  } catch {
    // Storage blocked (private mode): the choice just doesn't persist.
  }
}
