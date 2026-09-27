import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { SettingsResponse } from "../../types";
import { SettingsPanel } from "./SettingsPanel";

vi.mock("../../hooks/useMediaQuery", () => ({
  useMediaQuery: () => true,
}));

vi.mock("../ThemeProvider", () => ({
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

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Web search provider" }),
      "exa",
    );
    expect(screen.getByText("key required")).toBeInTheDocument();
    await user.type(
      screen.getByRole("textbox", { name: "Exa API key" }),
      "exa-key",
    );
    await saveSettings(user);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    expect(savedConfig(fetchMock).web).toEqual({
      fetch: "local",
      search: "exa",
      exa: { api_key: "exa-key" },
    });
  });
});
