import express from "express";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { z } from "zod";
import { renderMotion } from "./motion.js";
import { editVideo } from "./video.js";
import { uploadPublic } from "./storage.js";

const app = express();
app.disable("x-powered-by");
app.use(express.json({ limit: "256kb" }));
const PORT = Number(process.env.PORT || 10000);
const API_KEY = process.env.API_KEY;

const motionSchema = z.object({
  titles: z.array(z.string().trim().min(1).max(160)).min(1).max(8),
  primaryColor: z.string().regex(/^#[0-9a-fA-F]{6}$/).default("#6C5CE7"),
  durationSeconds: z.number().min(3).max(30).default(8),
  animationStyle: z.enum(["fade", "slide", "zoom"]).default("fade"),
  subtitle: z.string().max(300).optional(),
  width: z.number().int().min(640).max(1920).default(1920),
  height: z.number().int().min(360).max(1080).default(1080),
  fps: z.number().int().min(24).max(60).default(30)
});
const clipSchema = z.object({
  url: z.string().url().max(2048),
  startSeconds: z.number().min(0).default(0),
  endSeconds: z.number().positive()
}).refine(v => v.endSeconds > v.startSeconds, { message: "endSeconds must be greater than startSeconds" });
const editSchema = z.object({
  clips: z.array(clipSchema).min(1).max(8),
  audioUrl: z.string().url().max(2048).optional(),
  audioVolume: z.number().min(0).max(1).default(0.8),
  outputWidth: z.number().int().min(640).max(1920).default(1920),
  outputHeight: z.number().int().min(360).max(1080).default(1080),
  fps: z.number().int().min(24).max(60).default(30)
});
const requestSchema = z.discriminatedUnion("projectType", [
  z.object({ projectType: z.literal("motion"), motionProps: motionSchema }),
  z.object({ projectType: z.literal("edit"), editProps: editSchema })
]);

app.get("/health", (_req, res) => res.json({ ok: true, service: "cloud-video-engine" }));
app.post("/api/v1/render", async (req, res) => {
  if (!API_KEY || req.get("authorization") !== `Bearer ${API_KEY}`) {
    return res.status(401).json({ error: "unauthorized", message: "Missing or invalid bearer token." });
  }
  const parsed = requestSchema.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "invalid_request", details: parsed.error.flatten() });
  const jobId = crypto.randomUUID();
  const workDir = await fs.mkdtemp(path.join(os.tmpdir(), `video-${jobId}-`));
  const outputPath = path.join(workDir, "output.mp4");
  try {
    if (parsed.data.projectType === "motion") await renderMotion(parsed.data.motionProps, outputPath);
    else await editVideo(parsed.data.editProps, outputPath, workDir);
    const videoUrl = await uploadPublic(outputPath, `renders/${jobId}.mp4`);
    return res.json({ jobId, status: "completed", projectType: parsed.data.projectType, videoUrl, format: "mp4", message: "Rendering completed successfully." });
  } catch (err) {
    console.error(`[${jobId}] render failed:`, err?.stack || err);
    return res.status(500).json({ jobId, error: "render_failed", message: "Rendering failed. Check service logs and storage configuration." });
  } finally {
    await fs.rm(workDir, { recursive: true, force: true }).catch(() => {});
  }
});
app.use((err, _req, res, _next) => {
  if (err instanceof SyntaxError && "body" in err) return res.status(400).json({ error: "invalid_json", message: "Request body must be valid JSON." });
  console.error(err);
  res.status(500).json({ error: "internal_error" });
});
app.listen(PORT, "0.0.0.0", () => console.log(`Cloud Video Engine listening on ${PORT}`));
