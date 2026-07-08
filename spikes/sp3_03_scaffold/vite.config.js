import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base './' 必須: /api/demos/{id}/app/ 配下の相対パス配信(specs/19 §4.1 F3)
export default defineConfig({ plugins: [react()], base: "./" });
