/** Apple platforms use ⌘ where others use Ctrl for app shortcuts. */
export const isMac =
  typeof navigator !== "undefined" &&
  /Mac|iPhone|iPad/.test(navigator.userAgent);
