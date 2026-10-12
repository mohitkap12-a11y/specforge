/** "6", "0x06" or "06h" -> number; empty -> undefined; anything else -> null (invalid). Bare digits are decimal. */
export function parseOpcode(text: string): number | undefined | null {
  const t = text.trim().toLowerCase();
  if (!t) return undefined;
  const m = t.match(/^(?:0x([0-9a-f]+)|([0-9a-f]+)h|(\d+))$/);
  if (!m) return null;
  const n = m[1] ? parseInt(m[1], 16) : m[2] ? parseInt(m[2], 16) : parseInt(m[3], 10);
  return n >= 0 && n <= 255 ? n : null;
}

/** Set a nested value without mutating the input: setIn({a:{b:1}}, ["a","c"], 2) -> {a:{b:1,c:2}}. */
export function setIn<T extends Record<string, any>>(obj: T, path: string[], value: unknown): T {
  if (!path.length) return value as T;
  const [head, ...rest] = path;
  return { ...obj, [head]: setIn((obj?.[head] ?? {}) as Record<string, any>, rest, value) };
}

/** Bound a live log list. While following the tail (`following`) it is trimmed from the top to `soft`; while the user reads
 *  history (scrolled up, or after loading older events) nothing is dropped until a larger `hard` cap, so explicitly loaded rows stay visible. */
export function trimRows<T>(rows: T[], following: boolean, soft: number, hard: number): T[] {
  const max = following ? soft : hard;
  return rows.length > max ? rows.slice(rows.length - max) : rows;
}
