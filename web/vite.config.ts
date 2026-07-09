import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The Cloudflare Worker in ../worker serves the built assets from web/dist at the
// site root, so the default base ("/") is correct.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
