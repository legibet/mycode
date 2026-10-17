/**
 * Class name utility for merging Tailwind classes.
 */

import { type ClassValue, clsx } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

// Font sizes from index.css; unknown `text-*` classes would merge as colors.
const twMerge = extendTailwindMerge({
  extend: { theme: { text: ["2xs", "ui"] } },
});

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
