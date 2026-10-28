import { existsSync } from "node:fs";
import { cp } from "node:fs/promises";

const envFile = new URL("../../.env", import.meta.url);
if (existsSync(envFile)) process.loadEnvFile(envFile);

if (process.argv[2] === "dev") {
  // Native loading avoids passing --env-file through Next's worker NODE_OPTIONS.
  process.argv.push("--hostname", "127.0.0.1");
  await import("./node_modules/next/dist/bin/next");
} else {
  // Standalone output omits static assets; copy them before starting the server.
  await cp(
    new URL(".next/static", import.meta.url),
    new URL(".next/standalone/.next/static", import.meta.url),
    { recursive: true },
  );
  process.env.HOSTNAME ??= "127.0.0.1";
  await import("./.next/standalone/server.js");
}
