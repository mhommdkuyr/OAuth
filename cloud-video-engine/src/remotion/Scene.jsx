import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig } from "remotion";
export function MotionScene({ titles = ["Your title"], primaryColor = "#6C5CE7", animationStyle = "fade", subtitle = "" }) {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const progress = frame / Math.max(1, durationInFrames - 1);
  const opacity = animationStyle === "fade" ? interpolate(frame, [0, fps * .7, durationInFrames - fps * .5, durationInFrames - 1], [0, 1, 1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }) : interpolate(frame, [0, fps * .6], [0, 1], { extrapolateRight: "clamp" });
  const offset = animationStyle === "slide" ? interpolate(frame, [0, fps * .7], [100, 0], { extrapolateRight: "clamp" }) : 0;
  const scale = animationStyle === "zoom" ? interpolate(frame, [0, durationInFrames - 1], [.88, 1.04]) : 1;
  const title = titles[Math.min(titles.length - 1, Math.floor(progress * titles.length))] || "";
  return <AbsoluteFill style={{ background: `radial-gradient(circle at ${25 + progress * 50}% ${30 + Math.sin(progress * Math.PI * 2) * 10}%, ${primaryColor} 0%, #111322 62%, #080910 100%)`, color: "white", fontFamily: "Arial, Helvetica, sans-serif", justifyContent: "center", alignItems: "center", overflow: "hidden" }}>
    <AbsoluteFill style={{ opacity: .14, backgroundImage: "linear-gradient(rgba(255,255,255,.14) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.14) 1px, transparent 1px)", backgroundSize: "64px 64px", transform: `translateY(${progress * -80}px)` }} />
    <div style={{ maxWidth: "82%", textAlign: "center", opacity, transform: `translateY(${offset}px) scale(${scale})`, fontSize: "clamp(54px, 7vw, 132px)", lineHeight: 1.08, fontWeight: 800, letterSpacing: "-.045em", textShadow: "0 8px 36px rgba(0,0,0,.28)" }}>{title}</div>
    {subtitle ? <div style={{ position: "absolute", bottom: "16%", opacity: Math.min(1, opacity + .1), fontSize: 30, color: "#E6E7F0", textAlign: "center", maxWidth: "75%" }}>{subtitle}</div> : null}
    <div style={{ position: "absolute", bottom: 48, width: "70%", height: 5, background: "rgba(255,255,255,.18)", borderRadius: 99 }}><div style={{ height: "100%", width: `${progress * 100}%`, background: "white", borderRadius: 99 }} /></div>
  </AbsoluteFill>;
}
