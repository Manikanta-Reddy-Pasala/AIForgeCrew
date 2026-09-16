/**
 * Keyboard-reachable click targets.
 *
 * A `<div>` or `<a>` with an `onClick` and nothing else is invisible to anyone
 * driving the UI from the keyboard: it takes no focus, and Enter/Space do
 * nothing. Every fix is the same four props, so this is the one place that
 * knows them rather than seventeen copies that can drift apart.
 *
 * Use a real `<button>` when you can — this is for the cases where the element
 * has to stay a div/a for layout or styling reasons.
 */
import type { KeyboardEvent } from 'react';

export function clickable(onActivate: () => void) {
  return {
    role: 'button' as const,
    tabIndex: 0,
    onClick: onActivate,
    onKeyDown: (e: KeyboardEvent) => {
      // Enter and Space are what a native <button> responds to; preventDefault
      // stops Space from also scrolling the page.
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        onActivate();
      }
    },
  };
}

/**
 * Props for a modal BACKDROP: click or Enter/Space on the backdrop itself
 * closes the dialog.
 *
 * Only events whose target IS the backdrop count. A click inside the panel,
 * or a keystroke in one of its inputs, bubbles up to here too — the panels
 * used to swallow those with stopPropagation handlers of their own, which put
 * mouse and key listeners on a non-interactive element just to switch this
 * one off. Checking the target does the same job from the one place that
 * should own it.
 */
export function backdrop(onClose: () => void) {
  return {
    role: 'button' as const,
    tabIndex: 0,
    onClick: (e: { target: EventTarget; currentTarget: EventTarget }) => {
      if (e.target === e.currentTarget) onClose();
    },
    onKeyDown: (e: KeyboardEvent & { target: EventTarget; currentTarget: EventTarget }) => {
      if (e.target !== e.currentTarget) return;
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        onClose();
      }
    },
  };
}
