import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

export default defineConfig({
  plugins: [react()],
  base: "/atlas/",
  define: { __MAPA_APP_MODE__: JSON.stringify("atlas") },
  build: {
    outDir: "dist-atlas",
    emptyOutDir: true,
    rollupOptions: { input: { index: resolve(__dirname, "v3-atlas.html") } },
  },
});
