import type {
  ChatRequest,
  ChatResponse,
  GlobalConfig,
  RemoteConfig,
  RunEventPayload,
  SessionResponse,
  SessionsResponse,
  SettingsResponse,
  WorkspaceBrowseResponse,
  WorkspaceRootsResponse,
} from "../types";

interface APIResult<T> {
  ok?: boolean | undefined;
  status?: number | undefined;
  data?: T | undefined;
  detail?: unknown;
  OK?: boolean | undefined;
  Status?: number | undefined;
  Data?: T | undefined;
  Detail?: unknown;
}

interface WailsApp {
  GetConfig(cwd: string): Promise<APIResult<RemoteConfig>>;
  Settings(): Promise<APIResult<SettingsResponse>>;
  UpdateSettings(req: {
    config: GlobalConfig;
  }): Promise<APIResult<SettingsResponse>>;
  ListSessions(cwd: string): Promise<APIResult<SessionsResponse>>;
  LoadSession(sessionId: string): Promise<APIResult<SessionResponse>>;
  DeleteSession(sessionId: string): Promise<APIResult<{ status: string }>>;
  ClearSession(sessionId: string): Promise<APIResult<{ status: string }>>;
  StartChat(req: ChatRequest): Promise<APIResult<ChatResponse>>;
  CancelRun(runId: string): Promise<APIResult<{ status: string }>>;
  DecideRun(
    runId: string,
    req: { request_id: string; decision: "allow" | "deny" },
  ): Promise<APIResult<{ status: string }>>;
  SelectFiles(
    title: string,
    pattern: string,
    multiple: boolean,
  ): Promise<APIResult<SelectedFile[]>>;
  WorkspaceRoots(): Promise<APIResult<WorkspaceRootsResponse>>;
  BrowseWorkspace(
    root: string,
    path: string,
  ): Promise<APIResult<WorkspaceBrowseResponse>>;
}

interface SelectedFile {
  name: string;
  data: string;
  mime_type?: string | undefined;
}

export type DesktopCommand = "new_chat" | "select_workspace" | "open_settings";

const IMAGE_FILE_PATTERNS = [
  "*.png",
  "*.jpg",
  "*.jpeg",
  "*.gif",
  "*.webp",
  "*.svg",
  "*.bmp",
  "*.tif",
  "*.tiff",
  "*.heic",
  "*.heif",
  "*.avif",
];

declare global {
  interface Window {
    go?: {
      main?: {
        App?: WailsApp;
      };
    };
    runtime?: {
      EventsOn?: (
        name: string,
        callback: (payload: unknown) => void,
      ) => () => void;
      BrowserOpenURL?: (url: string) => void;
      WindowSetTitle?: (title: string) => void;
      OnFileDrop?: (
        callback: (x: number, y: number, paths: string[]) => void,
        useDropTarget?: boolean,
      ) => void;
      OnFileDropOff?: () => void;
    };
  }
}

export class APIError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown, fallback: string) {
    super(detailToMessage(detail, fallback));
    this.status = status;
    this.detail = detail;
  }
}

function isWails(): boolean {
  return Boolean(window.go?.main?.App);
}

function wailsApp(): WailsApp {
  const app = window.go?.main?.App;
  if (!app) throw new Error("Wails runtime is not available");
  return app;
}

function normalizeResult<T>(result: APIResult<T>): APIResult<T> {
  return {
    ok: result.ok ?? result.OK,
    status: result.status ?? result.Status,
    data: result.data ?? result.Data,
    detail: result.detail ?? result.Detail,
  };
}

async function callWails<T>(
  method: keyof WailsApp,
  fallback: string,
  ...args: unknown[]
): Promise<T> {
  const fn = wailsApp()[method] as (
    ...args: unknown[]
  ) => Promise<APIResult<T>>;
  const result = normalizeResult(await fn(...args));
  if (!result.ok) {
    throw new APIError(result.status || 500, result.detail, fallback);
  }
  return result.data as T;
}

async function fetchJSON<T>(
  url: string,
  fallback: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(url, init);
  const data = (await res.json()) as T | { detail?: unknown };
  if (!res.ok) {
    throw new APIError(
      res.status,
      isRecord(data) && "detail" in data ? data.detail : undefined,
      fallback,
    );
  }
  return data as T;
}

async function fetchOK(
  url: string,
  fallback: string,
  init?: RequestInit,
): Promise<void> {
  const res = await fetch(url, init);
  if (!res.ok) {
    let detail: unknown;
    try {
      const data = (await res.json()) as { detail?: unknown };
      detail = data.detail;
    } catch {}
    throw new APIError(res.status, detail, fallback);
  }
}

function detailToMessage(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail) return detail;
  if (
    detail &&
    typeof detail === "object" &&
    "message" in detail &&
    typeof detail.message === "string"
  ) {
    return detail.message;
  }
  return fallback;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object";
}

function acceptToDialogPattern(accept: string): string {
  const patterns = new Set<string>();
  for (const rawToken of accept.split(",")) {
    const token = rawToken.trim().toLowerCase();
    if (!token) continue;
    if (token.startsWith(".")) patterns.add(`*${token}`);
    if (token === "image/*") {
      for (const pattern of IMAGE_FILE_PATTERNS) patterns.add(pattern);
    }
  }
  return Array.from(patterns).join(";");
}

function base64ToBuffer(data: string): ArrayBuffer {
  const binary = atob(data);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

function isExternalDesktopURL(url: URL): boolean {
  return (
    url.protocol === "http:" ||
    url.protocol === "https:" ||
    url.protocol === "mailto:"
  );
}

function installWailsDesktopChrome(): void {
  if (!isWails()) return;

  document.documentElement.setAttribute("data-mycode-desktop", "wails");

  document.addEventListener(
    "click",
    (event) => {
      if (event.defaultPrevented) return;
      const target = event.target;
      if (!(target instanceof Element)) return;

      const anchor = target.closest<HTMLAnchorElement>("a[href]");
      if (!anchor) return;

      const url = new URL(anchor.href, window.location.href);
      if (!isExternalDesktopURL(url)) return;

      event.preventDefault();
      window.runtime?.BrowserOpenURL?.(url.href);
    },
    true,
  );

  window.runtime?.OnFileDrop?.(() => {}, false);
}

async function selectWailsInputFiles(input: HTMLInputElement): Promise<void> {
  const pattern = acceptToDialogPattern(input.accept);
  if (!pattern) return;

  const files = await callWails<SelectedFile[]>(
    "SelectFiles",
    "Failed to attach files",
    "Attach files",
    pattern,
    input.multiple,
  );
  if (!files.length) return;

  const transfer = new DataTransfer();
  for (const file of files) {
    transfer.items.add(
      new File([base64ToBuffer(file.data)], file.name, {
        type: file.mime_type || "",
      }),
    );
  }
  input.files = transfer.files;
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

function installWailsFileInputPicker(): void {
  document.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (
        !isWails() ||
        !(target instanceof HTMLInputElement) ||
        target.type !== "file"
      ) {
        return;
      }

      event.preventDefault();
      event.stopImmediatePropagation();
      selectWailsInputFiles(target).catch((error) => {
        console.error("Failed to attach files:", error);
      });
    },
    true,
  );
}

export const transport = {
  isWails,

  async getConfig(cwd: string): Promise<RemoteConfig> {
    if (isWails()) {
      return await callWails("GetConfig", "Failed to load config", cwd);
    }
    return await fetchJSON<RemoteConfig>(
      `/api/config?cwd=${encodeURIComponent(cwd)}`,
      "Failed to load config",
    );
  },

  async getSettings(): Promise<SettingsResponse> {
    if (isWails()) {
      return await callWails("Settings", "Failed to load settings");
    }
    return await fetchJSON<SettingsResponse>(
      "/api/settings",
      "Failed to load settings",
    );
  },

  async updateSettings(config: GlobalConfig): Promise<SettingsResponse> {
    const body = { config };
    if (isWails()) {
      return await callWails("UpdateSettings", "Failed to save settings", body);
    }
    return await fetchJSON<SettingsResponse>(
      "/api/settings",
      "Failed to save settings",
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
  },

  async listSessions(cwd: string): Promise<SessionsResponse> {
    if (isWails()) {
      return await callWails("ListSessions", "Failed to load sessions", cwd);
    }
    return await fetchJSON<SessionsResponse>(
      `/api/sessions?cwd=${encodeURIComponent(cwd)}`,
      "Failed to load sessions",
    );
  },

  async loadSession(sessionId: string): Promise<SessionResponse> {
    if (isWails()) {
      return await callWails(
        "LoadSession",
        "Failed to load session",
        sessionId,
      );
    }
    return await fetchJSON<SessionResponse>(
      `/api/sessions/${encodeURIComponent(sessionId)}`,
      "Failed to load session",
    );
  },

  async deleteSession(sessionId: string): Promise<void> {
    if (isWails()) {
      await callWails("DeleteSession", "Failed to delete session", sessionId);
      return;
    }
    await fetchOK(
      `/api/sessions/${encodeURIComponent(sessionId)}`,
      "Failed to delete session",
      { method: "DELETE" },
    );
  },

  async clearSession(sessionId: string): Promise<void> {
    if (isWails()) {
      await callWails("ClearSession", "Failed to clear session", sessionId);
      return;
    }
    await fetchOK(
      `/api/sessions/${encodeURIComponent(sessionId)}/clear`,
      "Failed to clear session",
      { method: "POST" },
    );
  },

  async startChat(req: ChatRequest): Promise<ChatResponse> {
    if (isWails()) {
      return await callWails("StartChat", "Failed to start task", req);
    }
    return await fetchJSON<ChatResponse>("/api/chat", "Failed to start task", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    });
  },

  async cancelRun(runId: string): Promise<void> {
    if (isWails()) {
      await callWails("CancelRun", "Failed to cancel run", runId);
      return;
    }
    await fetchOK(
      `/api/runs/${encodeURIComponent(runId)}/cancel`,
      "Failed to cancel run",
      { method: "POST" },
    );
  },

  async decideRun(
    runId: string,
    requestId: string,
    decision: "allow" | "deny",
  ): Promise<void> {
    const body = { request_id: requestId, decision };
    if (isWails()) {
      await callWails("DecideRun", "Failed to send decision", runId, body);
      return;
    }
    await fetchOK(
      `/api/runs/${encodeURIComponent(runId)}/decide`,
      "Failed to send decision",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
  },

  async workspaceRoots(): Promise<WorkspaceRootsResponse> {
    if (isWails()) {
      return await callWails("WorkspaceRoots", "Failed to load roots");
    }
    return await fetchJSON<WorkspaceRootsResponse>(
      "/api/workspaces/roots",
      "Failed to load roots",
    );
  },

  async browseWorkspace(
    root: string,
    path = "",
  ): Promise<WorkspaceBrowseResponse> {
    if (isWails()) {
      return await callWails(
        "BrowseWorkspace",
        "Failed to browse directory",
        root,
        path,
      );
    }
    const params = new URLSearchParams({ root });
    if (path) params.set("path", path);
    return await fetchJSON<WorkspaceBrowseResponse>(
      `/api/workspaces/browse?${params.toString()}`,
      "Failed to browse directory",
    );
  },

  onRunEvent(handler: (payload: RunEventPayload) => void): () => void {
    if (!isWails()) return () => {};
    return (
      window.runtime?.EventsOn?.("mycode:run_event", (payload) => {
        handler(payload as RunEventPayload);
      }) || (() => {})
    );
  },

  onDesktopCommand(handler: (command: DesktopCommand) => void): () => void {
    if (!isWails()) return () => {};
    return (
      window.runtime?.EventsOn?.("mycode:desktop_command", (payload) => {
        if (
          payload === "new_chat" ||
          payload === "select_workspace" ||
          payload === "open_settings"
        ) {
          handler(payload);
        }
      }) || (() => {})
    );
  },

  setWindowTitle(title: string): void {
    if (!isWails()) return;
    window.runtime?.WindowSetTitle?.(title);
  },
};

installWailsDesktopChrome();
installWailsFileInputPicker();
