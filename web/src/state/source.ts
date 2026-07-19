import { createMockSource } from "./mock";
import { createRealSource } from "./real";
import type { StateSource } from "./types";

/**
 * Everything above this line is a source; everything below it must never
 * care which. The daemon is the default. VITE_HER_MOCK=1 brings back the
 * compressed-time mock for working on the room without her.
 */
export function createSource(): StateSource {
  return import.meta.env.VITE_HER_MOCK === "1" ? createMockSource() : createRealSource();
}
