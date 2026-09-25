// FW 站构建配置（021 裁定：Preact + Vite，任务视图试点）
// - 多入口：tasks（后续页面渐进迁入 inputs）
// - 产物落 src/agent/server/static/fw/（子目录隔离，不碰 vanilla 三件）
// - 无 hash 文件名：NoCacheStatic 已禁缓存，hash 只增 diff 噪声（产物进 git）
// - dev proxy：/api 与共享静态资源转发到 FastAPI（8000），HMR 开发
import { resolve } from "node:path";
import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

export default defineConfig({
  plugins: [preact()],
  base: "/fw/", // 部署子路径：产物经 /fw/ 伺服，资源 URL 前缀必须一致
  build: {
    outDir: "../src/agent/server/static/fw",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        tasks: resolve(import.meta.dirname, "tasks.html"),
        memory: resolve(import.meta.dirname, "memory.html"),
        graph: resolve(import.meta.dirname, "graph.html"),
        notes: resolve(import.meta.dirname, "notes.html"),
      },
      output: {
        entryFileNames: "assets/[name].js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name].[ext]",
      },
    },
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
      // 共享资源（style.css / vendor/）仍由 FastAPI 伺服，dev 时转发
      "/style.css": "http://127.0.0.1:8000",
      "/vendor": "http://127.0.0.1:8000",
    },
  },
});
