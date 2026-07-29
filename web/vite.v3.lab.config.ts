import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

export default defineConfig({
  plugins: [react()],
  base: "/lab/",
  define: { __MAPA_APP_MODE__: JSON.stringify("lab") },
  build: {
    outDir: "dist-lab",
    emptyOutDir: true,
    rollupOptions: { input: { index: resolve(__dirname, "v3-lab.html") } },
  },
});
