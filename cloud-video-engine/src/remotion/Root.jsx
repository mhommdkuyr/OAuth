import React from "react";
import { Composition } from "remotion";
import { MotionScene } from "./Scene.jsx";
export const Root = () => <Composition id="MotionScene" component={MotionScene} durationInFrames={240} fps={30} width={1920} height={1080} defaultProps={{ titles: ["Your title"], primaryColor: "#6C5CE7", durationSeconds: 8, animationStyle: "fade", width: 1920, height: 1080, fps: 30 }} />;
