import fs from "node:fs/promises";
export async function uploadPublic(filePath, objectPath) {
  const url = process.env.SUPABASE_URL;
  const serviceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  const bucket = process.env.SUPABASE_STORAGE_BUCKET || "video-renders";
  if (!url || !serviceKey) throw new Error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be configured.");
  const base = url.replace(/\/+$/, "");
  const bytes = await fs.readFile(filePath);
  const encoded = objectPath.split("/").map(encodeURIComponent).join("/");
  const response = await fetch(`${base}/storage/v1/object/${encodeURIComponent(bucket)}/${encoded}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${serviceKey}`, apikey: serviceKey, "Content-Type": "video/mp4", "x-upsert": "true", "cache-control": "31536000" },
    body: bytes, signal: AbortSignal.timeout(120000)
  });
  if (!response.ok) throw new Error(`Supabase upload failed (${response.status}): ${(await response.text()).slice(0, 300)}`);
  return `${base}/storage/v1/object/public/${encodeURIComponent(bucket)}/${encoded}`;
}
