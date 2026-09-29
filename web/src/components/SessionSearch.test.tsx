import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionSearchHit } from "../types";
import { SessionSearch } from "./SessionSearch";

function hit(
  id: string,
  title: string,
  snippet: SessionSearchHit["snippet"] = null,
): SessionSearchHit {
  return {
    session: { id, title, updated_at: "2026-09-20T10:00:00Z" },
    snippet,
  };
}

function jsonResponse(results: SessionSearchHit[]): Response {
  return new Response(JSON.stringify({ results }), {
    headers: { "Content-Type": "application/json" },
  });
}

function renderSearch() {
  const onSelect = vi.fn();
  const onClose = vi.fn();
  render(
    <SessionSearch
      open
      onClose={onClose}
      cwd="/work/my app"
      activeSessionId={undefined}
      onSelect={onSelect}
    />,
  );
  return { onSelect, onClose, input: screen.getByRole("combobox") };
}

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: true,
      addEventListener() {},
      removeEventListener() {},
    })),
  );
});

describe("SessionSearch", () => {
  it("does not fetch for an empty query", async () => {
    const { input } = renderSearch();
    expect(
      screen.getByText("Type to search titles and messages"),
    ).toBeInTheDocument();
    await userEvent.type(input, "   ");
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches with encoded q and cwd after the debounce and highlights the match", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse([
        hit("s1", "Fix login", {
          before: "we should ",
          match: "Retry",
          after: " the request",
        }),
      ]),
    );
    const { input } = renderSearch();
    await userEvent.type(input, "retry & more");

    await screen.findByText("Fix login");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const url = new URL(String(fetchMock.mock.calls[0]?.[0]), "http://x");
    expect(url.pathname).toBe("/api/sessions/search");
    expect(url.searchParams.get("q")).toBe("retry & more");
    expect(url.searchParams.get("cwd")).toBe("/work/my app");

    const match = screen.getByText("Retry");
    expect(match.tagName).toBe("MARK");
    expect(match.parentElement).toHaveTextContent(
      "we should Retry the request",
    );
  });

  it("keeps the match visible by trimming long leading context", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse([
        hit("s1", "Long", {
          before: `${"x".repeat(60)}tail `,
          match: "needle",
          after: "",
        }),
      ]),
    );
    const { input } = renderSearch();
    await userEvent.type(input, "needle");
    const match = await screen.findByText("needle");
    const line = match.parentElement?.textContent ?? "";
    expect(line).toMatch(/^…x+tail needle$/);
    expect(line.length).toBeLessThan("x".repeat(60).length);
  });

  it("selecting a result calls onSelect and onClose", async () => {
    fetchMock.mockResolvedValue(jsonResponse([hit("s42", "Pick me")]));
    const { input, onSelect, onClose } = renderSearch();
    await userEvent.type(input, "pick");
    await userEvent.click(await screen.findByText("Pick me"));
    expect(onSelect).toHaveBeenCalledWith("s42");
    expect(onClose).toHaveBeenCalled();
  });

  it("shows the empty message only after a query finishes with no results", async () => {
    fetchMock.mockResolvedValue(jsonResponse([]));
    const { input } = renderSearch();
    expect(screen.queryByText("No matching chats")).not.toBeInTheDocument();
    await userEvent.type(input, "nothing");
    expect(await screen.findByText("No matching chats")).toBeInTheDocument();
  });

  it("ignores a stale response for an older query", async () => {
    const pending: ((results: SessionSearchHit[]) => void)[] = [];
    fetchMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          pending.push((results) => resolve(jsonResponse(results)));
        }),
    );
    const { input } = renderSearch();

    await userEvent.type(input, "old");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await userEvent.type(input, "er");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    pending[1]?.([hit("new", "Newer result")]);
    await screen.findByText("Newer result");
    pending[0]?.([hit("old", "Stale result")]);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByText("Stale result")).not.toBeInTheDocument();
    expect(screen.getByText("Newer result")).toBeInTheDocument();
  });

  it("selects the first result after the list is replaced", async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse([hit("a", "Alpha"), hit("b", "Beta")]),
      )
      .mockResolvedValueOnce(jsonResponse([hit("c", "Gamma")]));
    const { input, onSelect } = renderSearch();

    await userEvent.type(input, "ab");
    await screen.findByText("Beta");
    await userEvent.keyboard("{ArrowDown}");
    expect(screen.getByRole("option", { name: /Beta/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    await userEvent.type(input, "c");
    const gamma = await screen.findByRole("option", { name: /Gamma/ });
    await waitFor(() => expect(gamma).toHaveAttribute("aria-selected", "true"));
    await userEvent.keyboard("{Enter}");
    expect(onSelect).toHaveBeenCalledWith("c");
  });

  it("dims earlier results and blocks Enter while the next query loads", async () => {
    let release: (() => void) | undefined;
    fetchMock
      .mockResolvedValueOnce(jsonResponse([hit("a", "Alpha")]))
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            release = () => resolve(jsonResponse([hit("b", "Beta")]));
          }),
      );
    const { input, onSelect } = renderSearch();

    await userEvent.type(input, "a");
    await screen.findByText("Alpha");
    await userEvent.type(input, "b");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    const alpha = screen.getByRole("option", { name: /Alpha/ });
    expect(alpha).toHaveAttribute("aria-disabled", "true");
    await userEvent.keyboard("{Enter}");
    expect(onSelect).not.toHaveBeenCalled();

    release?.();
    const beta = await screen.findByRole("option", { name: /Beta/ });
    expect(beta).not.toHaveAttribute("aria-disabled", "true");
    await userEvent.keyboard("{Enter}");
    expect(onSelect).toHaveBeenCalledWith("b");
  });

  it("shows a quiet error when the request fails", async () => {
    fetchMock.mockResolvedValue(new Response("boom", { status: 500 }));
    const { input } = renderSearch();
    await userEvent.type(input, "x");
    expect(await screen.findByText("Search failed")).toBeInTheDocument();
  });
});
