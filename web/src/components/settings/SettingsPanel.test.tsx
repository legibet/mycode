import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { SettingsResponse } from "@/api";
import { SettingsPanel } from "@/components/settings/SettingsPanel";

vi.mock("@/hooks/useMediaQuery", () => ({
  useIsDesktop: () => true,
}));

vi.mock("@/components/ThemeProvider", () => ({
  useTheme: () => ({ theme: "system", setTheme: vi.fn() }),
}));

const settings: SettingsResponse = {
  path: "/home/user/.mycode/config.json",
  exists: true,
  config: {},
  options: {
    provider_types: ["google"],
    permission_levels: ["safe"],
    permission_modes: ["ask"],
  },
  env: {},
  provider_type_env_vars: {},
  provider_type_default_models: {
    google: ["gemini-3.6-flash", "gemini-3.1-pro-preview"],
  },
};

function renderSettings() {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(settings), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
  render(<SettingsPanel open onClose={() => {}} settings={settings} />);
  return { fetchMock };
}

async function saveSettings(user: ReturnType<typeof userEvent.setup>) {
  const saveButton = screen.getAllByRole("button", { name: "Save" }).at(0);
  if (!saveButton) throw new Error("Save button not found");
  await user.click(saveButton);
}

function savedConfig(fetchMock: ReturnType<typeof vi.spyOn>) {
  const request = fetchMock.mock.calls[0]?.[1] as RequestInit | undefined;
  return JSON.parse(String(request?.body)).config;
}

describe("SettingsPanel", () => {
  it("configures Exa search and persists its key", async () => {
    const user = userEvent.setup();
    const { fetchMock } = renderSettings();

    await user.selectOptions(screen.getByRole("combobox", { name: "Web search provider" }), "exa");
    expect(screen.getByText("key required")).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Exa API key" }), "exa-key");
    await saveSettings(user);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    expect(savedConfig(fetchMock).web).toEqual({
      fetch: "local",
      search: "exa",
      exa: { api_key: "exa-key" },
    });
  });

  it("drafts from the loaded settings, keeps edits through a refetch and resets on reopen", async () => {
    const user = userEvent.setup();
    const loaded = { ...settings, config: { compact_threshold: 0.6 } };
    const panel = (open: boolean, current: SettingsResponse | null) => (
      <SettingsPanel open={open} onClose={() => {}} settings={current} />
    );
    const threshold = () => screen.getByPlaceholderText("0.8");

    // Opened before the settings arrive.
    const { rerender } = render(panel(true, null));
    rerender(panel(true, loaded));
    expect(threshold()).toHaveValue(0.6);

    await user.clear(threshold());
    await user.type(threshold(), "0.7");
    // A background refetch hands over an equal but new response.
    rerender(panel(true, { ...loaded, env: { EXA_API_KEY: false } }));
    expect(threshold()).toHaveValue(0.7);

    rerender(panel(false, loaded));
    await waitFor(() => expect(screen.queryByPlaceholderText("0.8")).toBeNull());
    rerender(panel(true, loaded));
    expect(threshold()).toHaveValue(0.6);
  });
});
