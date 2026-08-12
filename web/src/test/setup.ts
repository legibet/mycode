import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

globalThis.ClipboardEvent ??= Event as unknown as typeof ClipboardEvent;
Range.prototype.getBoundingClientRect ??= () => new DOMRect();
Element.prototype.scrollIntoView ??= () => {};

afterEach(() => {
  cleanup();
});
