import { defineConfig } from "vite";

// 界面由 FastAPI 的 / 提供，静态资源走 /assets：产物输出到 app/static/ui，
// 资源路径统一带 /assets/ui/ 前缀，文件名带内容哈希以便升级后立即生效。
export default defineConfig({
  base: "/assets/ui/",
  oxc: {
    jsx: { runtime: "automatic", importSource: "preact" },
  },
  build: {
    outDir: "../app/static/ui",
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8599",
    },
  },
});
