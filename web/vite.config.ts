import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import type { UserConfig } from "vite";

export default {
  plugins: [react(), tailwindcss()],
  resolve: {
    tsconfigPaths: true,
  },
  optimizeDeps: {
    exclude: [
      "@shikijs/core",
      "@shikijs/engine-javascript",
      "@shikijs/langs",
      "@shikijs/themes",
      "shiki",
      "shiki/core",
      "shiki/engine/javascript",
      "shiki/langs",
    ],
  },
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
} satisfies UserConfig;
