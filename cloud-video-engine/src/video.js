import fs from "node:fs/promises";
import path from "node:path";
import { pipeline } from "node:stream/promises";
import { Readable } from "node:stream";
import ffmpeg from "fluent-ffmpeg";

const MAX_BYTES = 250 * 1024 * 1024;
async function downloadHttps(inputUrl, dest) {
  const parsed = new URL(inputUrl);
  if (parsed.protocol !== "https:" || parsed.username || parsed.password) throw new Error("Media URL must be HTTPS and contain no credentials.");
  const response = await fetch(parsed, { signal: AbortSignal.timeout(30000), redirect: "error" });
  if (!response.ok || !response.body) throw new Error(`Media download failed with HTTP ${response.status}`);
  if (Number(response.headers.get("content-length") || 0) > MAX_BYTES) throw new Error("Media exceeds 250 MB.");
  let bytes = 0;
  const limiter = new TransformStream({ transform(chunk, controller) { bytes += chunk.byteLength; if (bytes > MAX_BYTES) throw new Error("Media exceeds 250 MB."); controller.enqueue(chunk); } });
  const handle = await fs.open(dest, "w");
  try { await pipeline(Readable.fromWeb(response.body.pipeThrough(limiter)), handle.createWriteStream()); }
  finally { await handle.close().catch(() => {}); }
}
function ffmpegDone(command, output) {
  return new Promise((resolve, reject) => command.on("end", resolve).on("error", reject).save(output));
}
export async function editVideo(props, outputPath, workDir) {
  const normalized = [];
  for (let i = 0; i < props.clips.length; i++) {
    const clip = props.clips[i];
    const source = path.join(workDir, `source-${i}.media`);
    const target = path.join(workDir, `normalized-${i}.mp4`);
    await downloadHttps(clip.url, source);
    await ffmpegDone(ffmpeg(source).setStartTime(clip.startSeconds).duration(clip.endSeconds - clip.startSeconds)
      .videoFilters([`scale=${props.outputWidth}:${props.outputHeight}:force_original_aspect_ratio=decrease`, `pad=${props.outputWidth}:${props.outputHeight}:(ow-iw)/2:(oh-ih)/2`, `fps=${props.fps}`])
      .videoCodec("libx264").audioCodec("aac").outputOptions(["-pix_fmt yuv420p", "-ar 48000", "-ac 2", "-movflags +faststart"]), target);
    normalized.push(target);
  }
  const concatList = path.join(workDir, "concat.txt");
  await fs.writeFile(concatList, normalized.map(p => `file '${p.replaceAll("'", "'\\''")}'`).join("\n"));
  const joined = props.audioUrl ? path.join(workDir, "joined.mp4") : outputPath;
  await new Promise((resolve, reject) => ffmpeg().input(concatList).inputOptions(["-f concat", "-safe 0"])
    .outputOptions(["-c copy", "-movflags +faststart"]).on("end", resolve).on("error", reject).save(joined));
  if (props.audioUrl) {
    const audioExt = path.extname(new URL(props.audioUrl).pathname).slice(0, 8) || ".audio";
    const audioPath = path.join(workDir, `music${audioExt}`);
    await downloadHttps(props.audioUrl, audioPath);
    await ffmpegDone(ffmpeg(joined).input(audioPath)
      .complexFilter([`[1:a]volume=${props.audioVolume}[music]`])
      .outputOptions(["-map 0:v:0", "-map [music]", "-shortest", "-movflags +faststart", "-pix_fmt yuv420p"])
      .videoCodec("libx264").audioCodec("aac"), outputPath);
  }
}
