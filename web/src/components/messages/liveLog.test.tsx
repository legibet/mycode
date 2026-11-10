import { renderHook, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { useLiveLog } from "@/components/messages/liveLog";

it("drops a missing log's notice when it connects again", async () => {
  globalThis.fetch = vi
    .fn()
    .mockResolvedValueOnce(new Response(null, { status: 404 }))
    .mockReturnValue(new Promise(() => {})) as typeof fetch;
  const { result, rerender } = renderHook(({ url }) => useLiveLog(url), {
    initialProps: { url: "/log" as string | null },
  });
  await waitFor(() => expect(result.current.status).toBe("missing"));

  rerender({ url: null });
  rerender({ url: "/log" });

  expect(result.current).toEqual({ status: "streaming", text: "" });
});
