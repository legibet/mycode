/**
 * Keyboard-triggered opens should always focus the text field. Pointer opens
 * only do so on desktop-like inputs, where this will not open a virtual
 * keyboard.
 */
export function shouldAutoFocusTextInputOnOpen(
  openedWithKeyboard: boolean,
): boolean {
  if (openedWithKeyboard) return true
  if (
    typeof window === 'undefined' ||
    typeof window.matchMedia !== 'function'
  ) {
    return false
  }
  return window.matchMedia('(hover: hover) and (pointer: fine)').matches
}
