import { createMockSource } from "./mock";
import type { StateSource } from "./types";

/**
 * Everything above this line is mock; everything below it must never care.
 * When the daemon is wired, this returns an EventSource-backed StateSource
 * and no component changes.
 */
export function createSource(): StateSource {
  return createMockSource();
}
